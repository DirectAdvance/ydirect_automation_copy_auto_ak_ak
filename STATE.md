# Нейродиректолог — Состояние

> Читать ПЕРВЫМ в начале каждой сессии. Обновлять ПОСЛЕДНИМ перед выходом.
> Ошибки создания РК: сигнатуры/решения/что-помогло — **ERRORS_JOURNAL.md** (обязателен к заполнению при фиксах).

> Архив сессий старше 3 дней — **STATE_ARCHIVE.md** (ротация 2026-07-19: перенесены сессии 07-15, 07-14, 07-13). Правило ротации — см. CLAUDE.md.

## Сессия 2026-07-19 — Ревью+фиксы сервиса slepki + массовые правки структуры (6 пунктов) — ЗАДЕПЛОЕНО (частично)

**Ревью (read-only) → 2 раунда fix → 2 раунда verify (`direct_verifier`, оба ✅ после доработки).**

**Код-фиксы (коммиты `d1e09cb`, `f2d9244`/`f94ad43`, `83482ac`/`345eaea` в обоих репо — nested `direct/` + `home/seoadvanced`):**
- Critical: `slepki_store.assemble()` отдавал общий кэш без копии → отклонённая preflight правка уезжала на диск следующей успешной. Фикс: `mutable`-флаг, default = копия.
- `save_assets`: теперь пишет ДЕЛЬТУ per-ct (не union во все ct) — раньше стирал различия (scherbakova 81 набор → 1). Модель: читаем union+карта различий, пишем только add/remove поверх своего набора ct, нетронутый ct не переписывается. Плюс бэкап fail-closed, strict-чтение на записи (нечитаемый файл не считается пустым ct).
- `_slCampSave` (карточка кампании) писал в легаси ct-агрегат, который per-group паки не читают → job ok при нулевом эффекте. Переведён на `minus_shared` (единственный канал, который движок подмешивает всегда).
- Path traversal: `_safe_token` + `validate_scope`, 12 роутов. Fuse-гейт в `kontent_pack._read_lines_opt`.
- Чистка: `staging.json`, `pavlov.json.bak`, `_tmp_*`, эндпоинт `edit_callouts`, 2 worktree.
- Тест `test_slepki_source_manifest.py` починен под per-slepok store (был 3/4, стал 4/4).
- Доки: README/ARCHITECTURE/CLAUDE.md досинхронизированы со слоем :5023+worker и UI (`slepki_ui.js`/`slepki.html`).

**Массовые правки данных (17 part-файлов, review-first: proposal → apply → verify), коммиты `b9f3c47`+`fab730c`+`8a3b4d9` (оба репо):**
1. tp6 «Мастер кампаний»→«МК», tp7 «Товарная кампания»/«Товарка»→«ТК» — везде: title, item.t, group.name, И В КОДЕ (`create_set_plan.py:92 tp_label`).
2. Хвосты имён: срезаны NUM_DASH (108) + PLUS_CHAIN (14). `+ CRM` и буквенные коды (`- DM/CR/TA/ML/OT`) — **оставлены сознательно**: срезка давала 9 коллизий имён, это различители как кузов (Sedan/Cross/Liftback, тоже не тронут).
3. terehov/tp7: удалены 2 тестовых `ml3-test` item. Слепок terehov НЕ удалён целиком (было двоякое чтение задачи — уточнили).
4. Схлопнуто 5 групп дублей по gc+gk (2 chepelev + dmp/avtolajt_bu/sk_krs). ⚠️ 3 из 5 похожи на ДЕФЕКТ ГЕНЕРАЦИИ `gk` (не учитывается гео/ct) — Семён решил схлопнуть, зная это; факты по group/gc/gk зафиксированы в `.claude/sdd/slepki-massfix-applied.md` для будущего расследования, место в коде-генераторе НЕ найдено.
5. Коллизия после переименования: 2 item в salamahin/Мультибренд/tp6 стали одинаковыми (регион был только в имени группы, не в item.t) → дедуп превью `seen_c` (create_set_structure.py) прятал один. Развели по конвенции соседа. На СОЗДАНИЕ не влияло (`_build_name` берёт имя из group.name+oblast, не из item.t) — подтверждено verify.
6. Итог: 14892→14857 items (был путь: -8 первый раунд, +33 переименования во втором не меняли count). Откат 1 правки вне скоупа (`strip_dom_plan` в `create_set_plan.py`, была случайно внесена исполнителем) — вернули к `_is_site_domain_name`.

**Задеплоено:** `direct-slepki.service` + `direct-slepki-worker.service` рестартованы дважды (16:07, 20:36), оба раза md5 Mac==LXC101 сверен ДО рестарта, логи чистые, `assemble()`=17 директологов подтверждён на сервере.

**⚠️ НЕ рестартован `direct-create.service` (:5020, создание РК) — НАМЕРЕННО.** Код `kontent_pack.py` и `create_set_plan.py` (общие с этим процессом) изменён, но живой прогон создания РК НЕ выполнялся. Пока не рестартован — старый код в проде, blast radius ограничен слепками. **Перед рестартом `direct-create` — обязателен пробный прогон создания одной РК** (тестовый аккаунт), проверить что tp1-tp7 создаются, имена/дедуп («ТК - ТК - …» не должно быть) корректны.

**Не верифицировано живьём:** UI-панель «Ассеты» под логином (различия ct видны глазами?); первый реальный `save_assets` с записью в M3 (`_ssh_write_m3_map` ни разу не выполнялась в проде, смотреть `write.callouts.m3_ok`); создание РК после правок kontent_pack/create_set_plan.

**Открыто / следующая сессия:**
- Живой прогон создания РК → затем рестарт `direct-create.service`.
- Расследовать дефект генерации `gk` (3 группы дублей в dmp/avtolajt_bu/sk_krs) — факты в `.claude/sdd/slepki-massfix-applied.md`.
- В дереве ~30 файлов чужой незакоммиченной работы (copy-сервис, content editor) — не наши, не трогали, но занимают `git status`.

**Отчёты:** `.claude/sdd/slepki-review-report.md`, `slepki-fix-report.md`(+`-2.md`), `slepki-massfix-proposal.md`, `slepki-massfix-applied.md`(+`-2.md`), `slepki-docs-drift.md` (в `home/seoadvanced/direct/.claude/sdd/`).

## Сессия 2026-07-19 — UI_MAP / nginx split / copy canonical — ЛОКАЛЬНО

**Сделано:** обновлены `docs/UI_MAP.md` и `deploy/nginx-direct-location.conf`: карта больше не говорит,
что весь JS inline в `index.html`, указывает на `static/direct/automation.js`, фиксирует canonical
copy-страницу `/direct/automation/copy` и полный nginx split для content/copy/slepki/accounts/ai/autorules.
В `static/direct/automation.js` старый `?tab=copy` теперь редиректит на `/direct/automation/copy`;
`copy` убран из SPA-вкладок `/direct/automation`.

**Проверено:** агентами и локально: `node --check home/seoadvanced/static/direct/automation.js`,
сверка локального nginx-шаблона с `nginx -T` на LXC101, `nginx -t` успешен. Продовые listeners
`:5020/:5021/:5022/:5023/:5024/:5026/:5027` и сервисы active. Продовый warning duplicate
`server_name` исправлен: backup `seoadvanced.ru.bak.ar_api_20260717_223349` перенесён из
`sites-enabled` в `sites-available`, `systemctl reload nginx`, повторный `nginx -T` чистый.

## Сессия 2026-07-19 — Вкладка «Смена изображения» (content-editor) — ЗАДЕПЛОЕНО, работает на реальных данных

**Сделано:** новая админская вкладка «Смена изображения» на `/direct/automation/content` — бэкенд
(`content_images_routes.py`: inventory / upload / preview / replace_async, тип задания `image_replace`
в `direct_automation.content_jobs`) + UI (редизайн в две колонки с модалкой). Смок на реальном
аккаунте: 54 карточки, все превью живые, гейт подтверждения перед постановкой в очередь держит.
Документация — `CONTENT_EDITOR.md`, раздел «Вкладка Смена изображения».

**Попутно закрыт класс багов `UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY`** — шесть потерь при
full PATCH: быстрые ссылки, уточнения, `displayHref`, `customText` кнопки, `relevance_match`,
`ca_retargeting_condition`. Пять подтверждены живой записью с откатом; `button.customText` не
подтверждён — объектов на аккаунтах нет.

**Коммиты:** `b42c5fb`, `ffaec1a`, `884492a`, `1bfb7e0`, `7862eb7`. Часть UI уехала в чужой коммит
`97fa8e1` (перехват параллельным окном).

**Сломано / требует решения:**
1. **БД Victory недоступна С LXC101** — `timeout expired` даже при 30 с; с мака та же БД отвечает
   (52/100 коннектов). Ломает вкладки «Сверка цен», «Очередь», список пользователей в админке;
   вкладка «Смена изображения» не затронута. Нужно внешнее действие: сетевой путь
   LXC101 → `103.88.240.90:5432`.
2. **`porg-pvrbl7mh` — 2 неоткаченных побочных эффекта UAC full PATCH:** объявление
   `1915248839254163593` `inheritableCallouts` `CLEAR`→`INHERIT`; кампания `712714472`
   `organic_search_enabled` `null`→`true` (вероятно необратимо). Команда восстановления — в
   `ERRORS_JOURNAL.md`.
3. **`routes_content_editor.py` не закоммичен** — наши UAC-правки перемешаны с чужим рефакторингом
   pricecheck.

**Осталось:** подпись `thumb` вместо имени картинки (в работе); `RemoteDisconnected` при чанке
100 кампаний; `legs_reconcile` не покрывает UAC-only ключи; независимой проверки редизайна не было.

## Сессия 2026-07-19 — Probe 3 «Смена изображения» на porg-pvrbl7mh — ЖИВАЯ ЗАПИСЬ, ОТКАЧЕНО

**Санкция Семёна на запись. Мутаций 4** (2 Grid `UpdateAdaptiveTextAds` + 2 UAC full PATCH),
все откачены, оба снимка ДО совпали побайтово. Прод-код не менялся; прод-путь `run_image_replace`.
**Закрыто живьём 4 из 6 полей:** `inheritableCallouts` OVERRIDE+`calloutIds`-СПИСОК (главное — форма
держалась только на интроспекции, теперь отправлена и принята: 200, `validationResult:null`),
`displayHref`/`linkTail`, `bannerPrice`/`adPrice`, `ca_retargeting_condition` (+бонусом
`relevance_match` и `inheritableSitelinkSet` — снят блокер probe 2).
**НЕ закрыто, объектов на аккаунте НЕТ:** `button.customText` (button пуст у 0/2794 адаптивных),
`creativeIds` на Grid-пути (видео у 1/2794, и оно в UAC-владеемой кампании → `update_ad_images`
по нему не вызывается никогда; сохранность видео доказана только через UAC PATCH).
**Находки:** (1) UAC full PATCH меняет ПОРЯДОК `device_types` при идентичном множестве —
сравнивать как множество, иначе ложный mismatch; (2) probe оставил 2 осиротевшие картинки в
библиотеке (удаление = лишняя мутация + баллы, решение за Семёном).
ERRORS_JOURNAL: `GRID_RMW_AD_ASSETS_WIPED` и UAC-дополнение переведены 🟡 → ✅ подтверждено живьём.
Отчёт: `.claude/sdd/probe3-pvrbl7mh-report.md`, снимки `probe3-pvrbl7mh-{before,uac-before}.json`.

## Сессия 2026-07-19 — Зонд UAC-замены картинки (708193487) — BLOCKED, мутаций 0

**Сделано:** пред-полётная сверка прод-билдера `_uac_campaign_patch_payload` с браузерным эталоном
`_har/UAC_image_replace.json` (HAR той же кампании) на реальном detail из снимка ДО. Ключей 33 vs 33,
но состав разный → сработало стоп-условие задачи, PATCH НЕ отправлялся, аккаунт равен снимку ДО.
**Найдено:** `relevance_match` — риск подтверждён как материальный (кампания `MK_AT` с active-автотаргетингом,
full PATCH обнулил бы его; write-форма в HAR вырезана, вывести нельзя); `ecom` — НОВЫЙ пропущенный ключ
(попается веткой `keywords is None`, хотя браузер шлёт). Деривация `content_ids` зелёная (5/5, позиция 5).
**Осталось:** решение Семёна — неурезанный HAR для `relevance_match` ЛИБО зонд на расходной МК ЛИБО
санкция на партиал-only путь (правка `_uac_patch_campaign_texts`, вне рамок «probe без правок»).
Коммит `abb3fcf` (только ERRORS_JOURNAL). Отчёт: `.claude/sdd/probe-porg-gcegsszl-report.md`.

## Сессия 2026-07-18 (3) — Копир кабинетов: verify до 117/117 зелёных + само-лечение — ЗАДЕПЛОЕНО

**Задача Семёна:** имитация копирования `porg-psm5h7q6 → porg-lzjk6p5m` (метрика 110106702, город→Красноярск,
delete_drafts) через `direct-copy.service` :5022; копировать до нуля ошибок, все verify-чекбоксы зелёные.
**ВЫПОЛНЕНО: run 20 (job `75f1f3e50ce1`) = ok 117/117, mismatch 0.** Живьём подтверждено: 2 ex-флейк
кампании 712881877=2677 real, 712881880=8600 ключей (= источнику).

**Исправлено 5 дефектов** (см. ERRORS_JOURNAL, сигнатуры ниже; каждый проверен фактом/live):
- **promo CSRF-cold** (`promo.py`, `ac68625`): первый grid-`add` на свежем UacClient уходил без x-csrf-token
  → «тихий null» (ни id, ни ошибок); промо самой массовой кампании всегда падало (12 расхождений). Фикс:
  `_ensure_csrf()` прогрев + retry-on-empty. НЕ про RUB/amount (опроверг live).
- **callout union-over-add** (`copy_steps.py`, `deeb10b`): кампания с 0 уточнений у источника ошибочно
  получала union из 8. union теперь только глобальный фолбэк при пустом файле связи.
- **routing v5-кросс-чек** (`copy_engine.py`, `f4d9b05`): grid-typename флейкует («13 GdUnifiedCampaign» на
  реально TEXT_CAMPAIGN) → битый CopyCamp-снапшот (EOF@305) → падение ПОСЛЕ delete_drafts. Гейт по v5-Type.
- **keyword self-heal + sitelinks-retry** (`copy_verify.py`+`copy_engine.py`, `874dff7`): 2 крупные tp2/Поиск
  кампании под-копировались (ключи не оседали, v5 add вернул truthy Id, failed=0). auto_repair не имел
  ремонтёра keyword_count/sitelinks → repairs=0. Добавлен `_repair_keywords` (live keywords.get vs source,
  дозалив ≤900 батч) + идемпотентный sitelinks-retry. В run 20 sitelinks-retry создал 3 набора, привязал 12.
  ⚠️ Диагноз агента «v5 keywords.add фантомит на поиске» ОПРОВЕРГНУТ live: add оседает; лимит API 1000/запрос.

### ⚠️ Продолжение (2026-07-19): «117/117» была МАСКОЙ — живая 1:1-проверка вскрыла вырезание ключей

Семён потребовал **живую** сверку 1:1 (не verify). Вскрылось: verify показывал зелёное, а живой v5-счёт —
недокоп. Раскрутка (все проверено фактом):
- **verify НЕ врёт**: keyword_count читает из v5 авторитетно (copy_verify.py:466 «добираем ВСЕ кампании»).
  На момент сверки ключи РЕАЛЬНО были (v5=source), **вырезаны ПОЗЖЕ**.
- **Механика**: ключи, залитые в ОКНО СОЗДАНИЯ кампании, Яндекс частично вырезает через ~10-20 мин.
  Ре-add в ОСЕВШУЮ кампанию **держится** (монитор 819: 2705 стабильно 20 мин). Размер решает: маленькие
  оседают, очень крупные (>~6k ключей) — нет.
- **Фикс `d527832`**: `_copy_delayed_reverify` после осевшей сверки гоняет цикл `repair→пауза→re-verify`
  до устойчивого 1:1; выход по 2 чистым кругам подряд И после `_COPY_HEAL_MIN_SEC`=20 мин (иначе ложная
  сходимость до позднего вырезания). + откат keyword-батча 900→200 (900 усугублял вырезание).
- **Результат (run 23, живьём)**: **11/13 кампаний — точное 1:1**. Две мега-кампании (src 9958→live 5759,
  src 8600→live 1009) НЕ добиваются: часть ключей вообще без target-группы (баг маппинга групп на крупных),
  остаток мгновенно вырезается даже при ручном ре-add. **Похоже на потолок Яндекса на большие черновики-наборы**
  (задело бы и реального клиента). Развилка A(зафиксировать 11/13)/B(расследовать мега) — Семён выбрал A: документируем.

**#23 скорость (измерено `bbdcffe` тайминг фаз):** копия ~32 мин, хоги — keywords 598с и videos 392с.
Сделано: **параллельный prefetch видео `177f0f8`** (скачивание 392→18с). keyword-батч 200→900 давал ~3× на
ключах, но усугублял вырезание → откачен. Итого чистый выигрыш скорости пока скромный (видео upload доминирует,
не download). НЕ сделано (план Части B): пайплайн create→verify→repair внахлёст (идея Семёна), API-путь ‖
cookie-путь для смешанных аккаунтов, grid-постпроцесс в параллель, параллельные keyword-батчи, видео-upload.

**Коммиты сессии:** `ac68625 deeb10b f4d9b05 874dff7 db3eb1e bbdcffe 177f0f8 d527832`.

## Сессия 2026-07-18 (2) — Создание РК: цикл по всем типам сайта pavlov + перенос проверок — ЗАДЕПЛОЕНО

**Задача Семёна:** гонять создание по слепку pavlov на `porg-ozge4ntu` (счётчик 109986170) по ВСЕМ типам
сайта, между прогонами чистить черновики, чинить код по ошибкам. Критерии: (1) нет ошибок создания/добивки;
(2) время ≤ кампаний × 1.5 мин. **ОБА ВЫПОЛНЕНЫ.**

| Тип сайта | job_id | Кампаний | Упало | Время | Норматив |
|---|---|---|---|---|---|
| Мультибренд | `8efe8b835ac6` | 32/32 | 0 | 31.2 мин | 48 |
| Монобренд | `758b62b6f979` | 27/27 | 0 | 21.8 мин | 40.5 |
| С пробегом | `12a86a597c27` | 20/20 | 0 | 15.5 мин | 30 |

Монобренд и «С пробегом» прошли С ПЕРВОГО прогона → фиксы не подгонка под Мультибренд.

**Исправлено 7 дефектов** (все найдены на живых прогонах, каждый проверен direct_verifier до деплоя):
- `slepok_qa_run.py`: цель Метрики была ЗАШИТА константой (579905467 от чужого аккаунта) → падало 32/32.
  Теперь резолвится по счётчику (`_goal_vse_formy`). Добавлена опция `--site-type` (без неё нельзя гнать по одному типу).
- **Регрессия фильтра моделей** (`text_gen.py`, `create_set_text_builders.py`, `create_set_tp1_builders.py`):
  фильтр «чужих моделей» строился по ct-уровневому имени, а группы стали per-adgroup → для `lada_granta_liftback`
  дискриминатор «лифтбек» выбрасывал ВСЕ ключи. Анти-пустой гейт обманывался спецключом `---autotargeting`
  (при заливке он пропускается). Замер: пустых групп 16 → 6 → 2. Коммит `4c75cfb`.
- **Ретраи транзиентов** (`yandex_gateway.py`): не было вообще, маркеры только англоязычные («Сервис временно
  недоступен» не подпадал). Доработка `ae42f92`: `add`-методы НЕ ретраятся после ОБРЫВА СВЯЗИ (риск дублей —
  сервер мог применить запрос), Direct-returned error ретраится для любого метода.
- **city_morph «Москвич»** (`6017114`): стем `москв` съедал марку → группы Москвича пустые. Матч теперь на уровне
  слова (`_NON_CITY_STEMS`). Регресс-проверка: 6 слепков × 5 городов, ~410k ключей, дельта только москвич.
- **Ложный `NO_ADPRICE_LIVE`** на товарке-only: гейт опирался на ключи `shopping_ads`/`listing_ads`, которых
  в live-counts НЕТ. Заменён на `adaptive_total == 0`.
- Врущая диагностика: `image_groups` считал item'ы с пустыми `image_hashes`; `images_uploaded` не инкрементился.

**Перенос проверок из копирования** (`11ab7b6` + `3c526d3`, обе проверки ✅) — 0 новых запросов/баллов
(данные уже приходили в `CampaignsEditData`, `read_campaign_invariants` их выбрасывал):
- `CALLOUTS_MISSING_LIVE` (tp1–tp5, tp6/7 не флагаются), `SITELINK_SET_MISSING_LIVE`, все report-only.
- `PROMO_MISSING` оживлён **ДВУХСТУПЕНЧАТО** (требование Семёна): ступень 1 = библиотека аккаунта
  (`bool(promos_all)` из уже сделанного v5 `promotions.get` в `attach_or_create_promo`, проброс через 4 слоя),
  ступень 2 = наличие в кампаниях. Аккаунт без промо → не флагается вообще.
  ⚠️ Первая версия была ПРОКСИ по набору и НЕ ловила главный сценарий 0/N — поймал проверяющий.

**Живая верификация новых кодов — ЗАКРЫТА фактом** (контрольный прогон `633798f99dba`, PASS 20/20):
прямое чтение кампании 712878108 → `campaign_assets_read=True`, `callout_ids`=8, `sitelink_set_id='1494624974'`,
`promo_extension_id='1914982'`. Проверки читают поля реально; молчат потому, что ассеты на месте.

**Осталось / решения Семёна:**
- Контент-пак pavlov неполон: 106 ct без картинок (у марок только Dongfeng, MG), `pavlov__jac_общее.txt` пуст.
  Решение Семёна: **в критерий приёмки НЕ засчитывать**, дособор — отдельная задача.
- Гео-фильтр не ловит «московский»/«подмосковье» (стем `москв` их не матчит) — пред-существующий пробел, в журнале.
- Чужой коммит `8326890` (`copy_engine.py:192`, параллельная сессия): убран фолбэк `or src_domain` → при
  отсутствии `_copy_source_domain` в body домен пустой. Не проверялось, не наша ветка.
- `enrich_errors` при Grid-чтении: `shopping_bodies: Validation error (FieldUndefined@[client/ads/rowset/bodies])`
  — существующая ошибка запроса, на новые проверки не влияет.

Ledger сессии: `.claude/sdd/progress.md`. Отчёты: `.claude/sdd/create-massfix-4-report.md`,
`city-morph-moscvich-report.md`, `verify-transfer-report.md`.

## Сессия 2026-07-18 — Сервис копирования: verify+repair+API+live-имитация — ЗАДЕПЛОЕНО

**Задеплоено на direct-copy.service (:5022) + закоммичено.** Коммиты: home `ecd7aa7`, work/slepki `2091729` (ветка `fix/direct-copy-negatives-images-20260717`).

- **copy_verify.py** (NEW): сверка источник↔цель 19 измерений (report-only) + 2 гео-измерения. `snapshot_transformed` различает ЕПК (замещённый снапшот → реальные метрики) и v5 (сырой → EXCLUDED, не ложный mismatch).
- **Авто-ремонт #12** (`run_copy_repair` в copy_verify.py, вшит в `_copy_cookie_postprocess`): D3 библиотеки минус-слов + D19 товарные фильтры — только ADD, идемпотентно, баллы агентства, best-effort. D10 аудитории/D14 кнопки нечинимы (нет writer у Директа: interest_ids 403, мутатора кнопки нет).
- **UI-отчёт #12** (copy_common.js/css): живой отчёт verify+repair на карточке джобы + аннотация кнопки-чеклиста. Читает `result.cookie_postprocess.copy_verify`.
- **copy_api.py** (NEW): программный API `/api/v1/copy/{start,status,campaigns}` — X-API-Key fail-closed (hmac), SSRF-guard, CORS-whitelist, geo_region_ids-валидация. Ключ `COPY_API_KEY` в `.secret/.env` на LXC101.
- **Гео #18**: инлайн-замена ключей ЕПК + нормализация тире `_REGION_ALIASES`. Проверено direct_verifier ✅.
- **Фид #16**: source нет в цели → replace/upload/skip; `copy_feed_upload.py` со сменой домена.
- **2 блокера копирования исправлены** (work/slepki `direct_copy.py`): (1) `negativekeywordsharedsets.get` FieldNames убран невалидный `"Type"` (API 8000, блокировал весь копир — также в copy_login.py, pull_directologist.py); (2) `NegativeKeywordSharedSetIds` в campaigns.add/adgroups.add обёрнут в `{"Items":[...]}` (сырой массив → 8000; давал 8/15 кампаний вместо 15/15).

**Live-имитация porg-psm5h7q6→porg-lzjk6p5m (Красноярск, delete_drafts), 4 прогона:**
- ✅ **15/15 кампаний созданы**, спот-проверка v5 подтвердила контент на цели: tp1/tp2/tp5 по 27-28 групп, 10-63 объявл, 2705-3279 ключей. Гео→Красноярск #11309, adPrice 15/15 из target-фида, метрика counter/goal, ключи 38424 через Grid (0 баллов), картинки ремаплено 118, удаление ТОЛЬКО черновиков.
- ⚠️ **ОСТАЛОСЬ 2 глубоких (по 1 неуспешной попытке фикса — НЕ добиты, не re-run вслепую):**
  1. **verify частично ненадёжен для свежих черновиков** — ЧАСТИЧНО ПОЧИНЕНО: v5-фолбэк в build_target_profile (copy_verify.py:453+) теперь триггерит на 0 КОНТЕНТА (не 0 групп) + пагинация → прогон 5: ok 36→43, **adgroup_count все 13 OK**, keyword_count 7 OK, unreadable по ключам исчез. ОСТАЛОСЬ (хвост): 6 kw-mismatch у кампаний с частичным Grid-stat-счётчиком (фолбэк для них не триггерился — Grid дал ненулевой счётчик); callouts/sitelinks/images verify через v5 НЕ добирает → нужно per-dim v5-добор (adextensions.get, adimages). Спот-проверка v5 подтверждает контент на цели — mismatch ложные.
  2. **Промо не привязываются — ROOT-CAUSE НАЙДЕН (фикс = реструктуризация #24):** промо СОЗДАЮТСЯ через grid addPromoExtensions (10 шт, ОК), но `step_attach_promos` (copy_steps.py:487) падает в `fallback_single` из-за **ID mismatch**: v5-путь (copy_engine.py:2223) ключует `maps["promotions"]` по `promotions.json.Id`, а `campaign_promos.json` пишется по `promoExtension.id` из `campaigns_edit_rows` → by_promo пуст → фолбэк (при 10 промо не привязывает). Прогон 7 подтвердил: `campaign_promos.json заполнен из source edit_rows (13)` НО `исходная связь недоступна`. **ФИКС:** реструктурировать v5-промо-блок как UAC-блок (copy_engine.py:1640) — создавать промо ИЗ edit_rows-defs, тогда maps ключуется по promoExtension.id и совпадает. Half-fix (только запись campaign_promos.json) откачен — инертен.
  3-septies. **ФИНАЛЬНЫЙ РАЗБОР остатка 32 (после run 15, ok 85/117):**
     - **keyword ROOT-CAUSE (direct_copywriter + проверка деплоя):** РЕАЛЬНЫЙ баг — Grid `addKeywords`
       для tp2/поиск TextAdGroup-групп (v5 adgroups.add, НЕ ЕПК) рапортует успех (non-empty addedItems,
       `n_added==len(rows_b)`) но **НЕ персистит ключи**. `n_added` фикс (copy_steps.py:919, из прошлой
       сессии) ПРИСУТСТВУЕТ и задеплоен, но НЕДОСТАТОЧЕН — run-15 с ним via_v5=0 (Grid не отдаёт `[]`,
       отдаёт ложный success). tp1-РСЯ-группы Grid принимает (мигрированы в ЕПК?). **ФИКС #24:** роутить
       ключи поиск/TextAdGroup-групп сразу в v5 keywords.add (Grid для них не работает) ИЛИ post-add
       re-read верификация в step_keywords. Отчёт: `.claude/sdd/copy-search-keywords-report.md`. Глубокая
       правка + тесты (РСЯ-укладка рабочая, риск регрессии) — НЕ трогать без валидации.
     - **keyword МЕХАНИКА (трассировка):** group-mapping КОРРЕКТЕН (28/28 групп→кампания 712878852), но
       каждая target-группа получила **ровно 1 ключ** (source ~96/группа). Имена — **МК-формат
       `ct00NN_aon_..._ct001_ag011`** (Master Campaign tp6), хотя source помечена tp2-Поиск. → либо
       МК-семантика (1 keyword-set/группа = target верен, source-счёт несопоставим — тогда это verify-баг
       сравнения), либо dedup гео-вариаций. **Нужна доменная экспертиза слепка (direct_slepki_master)** —
       слепая правка step_keywords сломает рабочую РСЯ-укладку. НЕ трогать без понимания МК-структуры.
     - **keyword(6)+sitelinks(2): РЕАЛЬНЫЙ баг** — ПОИСКОВЫЕ кампании (tp2/tp5) получают ~0 ключей
       (Grid И v5 читают 0, keywords_read=true → не read-баг, реально не легли), при том что step_keywords
       рапортует via_grid 38424/548 без группы. Ключи ушли не в те группы → **баг group-matching поисковых
       кампаний в step_keywords** (copy_steps.py). РСЯ (tp1) ключи получают норм. Требует копи-энджин фикса.
     - **callout(12): verify-limitation** — 50 callout-расширений associated на цели (привязаны), но v5
       (ads AdExtensions=0, campaign CalloutIds/inheritableCallouts=0) не читает Grid-привязку per-campaign.
       Копия корректна; verify нужно читать Grid-путём копира.
     - **promo(12): ВНЕШНИЙ ЕРИР-БАРЬЕР** — grid addPromoExtensions создаёт 1 из 2 промо (ЕРИР блокирует),
       attach 1/13. **Не код-фиксимо** — внешняя механика Яндекса. → литеральный «ok:117» недостижим,
       пока ЕРИР блокирует создание промо (нужно внешнее действие/обход на стороне Директа).
  3-sexies. **ИТОГ RUN 15 (pristine + все фиксы): ok 42→85/117, копия ПОЛНАЯ (15/15, все типы ad).**
     Зазеленели после фиксов: keyword/adgroup/adaptive_titles/bodies/ads_with_images/sitelinks(11)/
     shared_set. **Остаток 32 — генуинно-тяжёлый хвост:** (1) **callout(12)** — контент ЕСТЬ (50 callout-
     расширений associated на цели), но verify Grid-read (edit_rows.inheritableCallouts=0) не мапит
     per-campaign Grid-привязку → verify-read-limitation, копия корректна; (2) **promo(12)** — РЕАЛЬНЫЙ
     пробел: grid addPromoExtensions создаёт только 1 из 2 промо (ЕРИР-область), attach 1/13; (3)
     **keyword(6)** — вариативность/остаток «без группы»; (4) **sitelinks(2)** — UAC/товарка легитимно без
     sitelinks. → #24. Копия-деливерабл КОРРЕКТНА; хвост = verify-read Grid-extension + ЕРИР-промо + UAC.
  3-quinque. **RUN 14-15: имитация вскрыла 2 РЕАЛЬНЫХ бага копии (verify верно ловил, НЕ eventual-consistency):**
     из `_upload_log.json`: (1) **LISTING_AD 0/27** — `FeedFilterConditions` передавался `{"Items":[...]}`,
     а `ads.add` ждёт МАССИВ (8000 «должен содержать массив»). Фикс direct_copy.py:1454 — разворот `.Items`
     (как copy_engine shopping-путь). (2) **TEXT_AD 9/27** — после гео-замены Красноярск (длиннее Кемерово)
     Title>56 → `ads.add` 5001 → объявление не создаётся. Фикс direct_copy.py:1331 — усечение Title/Title2/
     Text к 56/30/81. Также source_grid пересобирается в delayed re-verify (иначе fallback недочитывает
     адаптивы источника). Валидация — run 15 (pristine аккаунт: удалены кампании + картинки). Промо/callout
     остаются Grid-extension verify-чтениями (#24). **Ключевой урок: gsheet-адаптивы/LISTING_AD теряются
     тихо — imitation обязательна для полноты.**
  3-quater. **RUN 14 (чистый аккаунт + все фиксы) — ПРОРЫВ: ok 35→59, keyword_count ВСЕ 13 OK.**
     Найдены и устранены 3 корня: (а) **split-регрессия** `_norm_region_alias_key` (run 13 падал —
     перенёс в copy_geo + ре-экспорт); (б) **verify batch-4001** read-баг (ads/keywords.get на смешанных
     типах TextCampaign+UAC → 4001 → 0) — починен per-campaign; (в) **тест-артефакт: 1421 орфанная
     картинка** на цели (лимит аккаунта ~1000, накоплено 12 прогонами) → каскад на text-ad/images —
     **АККАУНТ ОЧИЩЕН (1421 удалено)**. Итог run 14: 15/15, keyword/adgroup зелёные, adPrice 132/132,
     адаптивы 260/260. Остаток 58 mismatch — verify-read-хвост (копия полная): (1) SOURCE read-баги
     ads_with_images/shared_set (src читается 0); (2) Grid-extension чтения promo(12)/callout(12)
     (verify не читает Grid-привязки через v5); (3) adaptive_titles семантика (src27→tgt9); (4) sitelinks
     2 (UAC/товарка легитимно). → #24 (scoped verify-хардининг, копия уже корректна).
  3-tris. **⚠️ КОРРЕКТИРОВКА (важно): гипотеза eventual-consistency ОПРОВЕРГНУТА.** Красные
     чекбоксы — НЕ гонка индексации, а: (1) **batch-4001 read-баги verify** — `ads.get`/`keywords.get`
     с батчем всех 13 CampaignIds на смешанных типах (TextCampaign+UAC/товарка) даёт API 4001 → 0
     → ложный mismatch. **ПОЧИНЕНО per-campaign** (коммиты после 0acb23e): на осевшей run-12 ok 35→52,
     sitelinks mismatch 12→2. (2) grid-extension чтения promo(13)/callout(12) — verify их v5 не читает
     (Grid-управляемые). (3) семантика adaptive_titles (src27→tgt9). (4) **реальные пробелы копии:**
     часть кампаний run-12 получила 0 ключей (camp 712876411: 0 keywords обоими v5-методами при 27
     группах) — Grid-first ключи не легли для части кампаний, ОТДЕЛЬНЫЙ баг копии. Остаток → #24.
  3-bis. **(прогон 11-12): копия по большинству кампаний корректна + адаптивный re-verify построен.**
     Спот-проверка v5 run-11: sitelinks НА цели — camp 712876027=63 объявл/**9 sitelinks**, camp
     712876029=218 объявл/**46 sitelinks**. Контент оседает **5-10+ мин ПОСЛЕ done** (dcr-демон
     `direct-create-worker` + async-индексация). Re-verify +300с (run 11) их не увидел, спот-проверка
     позже = увидел → построен **АДАПТИВНЫЙ отложенный re-verify** (copy_engine.py `_copy_delayed_reverify`
     + `_copy_target_sitelinks_ready`): поллит цель до появления sitelinks (до 15 мин), затем гонит
     полную copy_verify и перезаписывает результат job'а (UI читает осевшее). Механизм пере-записи
     подтверждён (run 11: `copy_verify (осевший, +300s)` отработал). Валидация адаптивной версии — run 12.
  3. **verify «не все зелёные» — КОРЕНЬ ЖЕЛЕЗОБЕТОННО ДОКАЗАН (10 прогонов, 2 settle-wait):** in-job settle-wait 150с (прогон 9) И 240с (прогон 10) → ОБА `sitelinks у 0 камп`, а спот-проверка пост-джоб = 9. Причина: dcr-демон привязывает контент через **`run_after_seconds:180` ПОСЛЕ статуса `done`**, а verify бежит ДО `done` → сколько ни жди В джобе, dcr ещё не стартовал. **In-job verify архитектурно НЕ МОЖЕТ увидеть dcr-контент.** Ниже — детали (совпадает): `_copy_cookie_postprocess` (v5-путь) НЕ привязывает sitelinks в джобе — их доливает **отложенный демон `delayed_content_repair`** (`_delayed_repair_daemon_loop`, `run_at` ПОСЛЕ джобы, automation_runtime.py:378, `_run_delayed_content_repair`). verify бежит В джобе → структурно НЕ видит dcr-привязки. Прогон 9: ждали 150с — sitelinks `у 0 камп` (контента ещё нет), спот-проверка через минуты = 9 (демон отработал). **In-job settle-wait откачен как бесполезный.** ФИКС (#24): вызвать `run_copy_verification` в конце `_run_delayed_content_repair` (после демона) + записать в job. Ad-level v5-добор sitelinks/images корректен (f880ec3), просто на in-job-моменте контента нет. Промо: привязка per_source_link починена (f880ec3), verify её на своём моменте тоже не видит. ok застрял на 43 через 8 прогонов. Реальные пробелы поверх (не dcr): images 9/27 (лимит картинок аккаунта — тест-артефакт от прогонов), ~548 ключей «без группы». **Копия корректна — спот-проверка v5: 15/15, группы/ключи/объявления/sitelinks/промо на цели.**
- **#19 монолит — СДЕЛАНО (коммит 7afbfc7):** copy_engine.py **3343→1659 строк**, byte-identical AST-распил на 10 модулей (copy_jobs/geo/snapshot/images/metrika/feeds/grid_read/uac/cleanup/grid_steps) через `direct-copy-engine-refactor/dev/split_tool.py`. DI-фан-аут: `configure()` раздаёт deps суб-модулям (у каждого свой `globals().update`). Гиганты (_copy_cookie_postprocess 447, _copy_grid_unified_campaigns 470, _copy_run_job 348 + delayed re-verify) оставлены в hub — высокая DI-связность / затрагивают новый код, отдельным заходом. Проверено: импорт через automation_runtime + DI всех 10 модулей True + сервис active, page 302/api 401, ошибок импорта нет.
- **#23 перф — НЕ начато:** замер postprocess+verify ~7мин, upload ~6мин, ключи ~5мин, pull ~2-3мин из ~19мин; план — параллельные подзагрузки Grid (rate-limit-aware), отдельным заходом.
- **#24 verify-after-settle — частично (адаптивный re-verify построен, коммит 0acb23e):** остаётся cron-проход через 15-30 мин (Яндекс осел) для честно-зелёных чекбоксов + чинить 548 ключей без группы + лимит картинок аккаунта (тест-артефакт от 12 прогонов).

## Сессия 2026-07-18 — Гео-замена ключей ЕПК + честность verify-метрик — ЗАДЕПЛОЕНО (в составе сессии выше)

- **Что сделано:** 3 блока правок в `copy_engine.py` + hint-обновление `copy_verify.py`.
  1. **Minor dash**: `_norm_region_alias_key` + `_REGION_ALIASES_NORM` — нормализация en/em/figure дашей в lookup ХМАО. Источник: API может вернуть en-dash U+2013 вместо em-dash U+2014 из dict-ключа.
  2. **Snapshot обогащение**: `snap_keywords_json` аккумулятор + `NegativeKeywords` в `snap_adgroups_json` (уже гео-заменённые/отфильтрованные) + запись `keywords.json` в синтетический snapshot ЕПК-пути.
  3. **geo-честность**: `check_geo_kw_consistency(src_dir, replacements)` вызывается в конце `_copy_grid_unified_steps` → `rep["geo_kw_consistency"]`. Теперь `geo_kw_source_residual` и `geo_neg_target_blocked` показывают РЕАЛЬНЫЕ данные для ЕПК-пути.
  4. **Комментарий `skipped`**: уточнён — ключи в ЕПК гео-заменяются ИНЛАЙН в group_specs[:1237] перед create_full (step_keywords скипается чтобы не создавать дубли, а не потому что замена не нужна).
- **Верификация:** py_compile + pyflakes: 0 новых предупреждений. НЕ деплоено (по условию задачи).
- **Осталось:** live-тест реального ЕПК-копирования с geo-заменой для проверки `geo_kw_consistency` в результате.

## Сессия 2026-07-17 — Мастер-поток баллов копирования: агентский пул — КОД ГОТОВ, НЕ ДЕПЛОЕНО

- **Что сделано:** `AuthContext.headers()` в `work/slepki_direktologov/scripts/direct_copy.py:227` — добавлен `"Use-Operator-Units": "true"` в else-ветку (токен-режим). До правки `phase_pull`/`phase_upload` (campaigns.add, adgroups.add, ads.add, keywords.add, feeds, images и т.д.) сжигали пул КЛИЕНТА; после — агентский пул, как в `yandex_gateway._headers()`.
- **Затронуто:** только `direct_copy.py`. `copy_engine.py` не тронут. Фолбэк на куки при 152 сохранён (`direct_call:259-263 → switch_to_cookie`). ЕПК-ветка (grid, 0 баллов) — не затронута.
- **Верификация:** py_compile + pyflakes OK; 4 pre-existing pyflakes предупреждения, 0 новых. Live-прогон по условию задачи не требовался.
- **НЕ деплоено:** правка в `work/` (Mutagen синкает на LXC101), `direct-copy.service` рестарт нужен при деплое.

## Сессия 2026-07-17 — Автоправила Фаза 4: Правила/Корректировки/Запросы/Площадки/Журнал — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** `rules_engine.py` (dry-run ЕСЛИ→ТО против Victory-метрик, `_AUTO_EXEC_ENABLED=False`); `corrections.py` (bidmodifiers.get/set чанками); `queries.py` (Reports SEARCH_QUERY + add_negative_phrases); `placements.py` (Reports PLACEMENT + log_excluded_sites — v5 REST не поддерживает прямое исключение, только audit_log + инструкция); `repository.py` — `audit_log_list` добавлен + `$1→%s` psycopg2-фикс; `autorules_main.py` — Phase 4 deps; `routes_autorules.py` — 8 новых маршрутов; `autorules.html` — 5 панелей + ~400 строк JS.
- **Верификация:** CRUD-тест на живой seoadvanced БД — все 6 шагов OK. py_compile + AST OK. `direct-autorules.service` active, все 6 новых маршрутов — 401 (auth), не 404/500.
- **Ограничение:** Площадки РСЯ — только log-only (v5 REST прямого исключения не поддерживает).
- **Коммит:** f331e41 (home-репо, 8 файлов фазы 4).
- **Осталось:** live-тест за Auth-куком и скриншоты UI — при следующем сеансе с аккаунтом.

## Сессия 2026-07-17 — Автоправила Фаза 3: копирование 1:1 внутри аккаунта — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** `direct/autorules/copy.py` (новый standalone-модуль: `list_campaigns` + `clone_campaigns_1to1`, v5/v501 напрямую, State=OFF enforced). Два роута в `routes_autorules.py`: `GET /api/ar/copy/campaigns` + `POST /api/ar/copy/run`. Вкладка «Копирование» в `autorules.html` упрощена: убраны блок трансформации, замена фидов, донор — оставлен бейдж аккаунта + таблица выбора + кнопка «Копировать 1:1».
- **Тест porg-ro552oi2:** `list_campaigns` вернул 2 кампании; `clone_campaigns_1to1(..., dry_run=True)` вернул корректный preview обеих (src_id, name+суффикс, type=TEXT_CAMPAIGN, ok=None). Маршрут 401 на `/api/ar/copy/campaigns?login=...` без куки = ожидаемо (auth check, не 404). `direct-autorules.service` active, journal чистый.
- **Ограничение MVP:** копирует только оболочку кампании (стратегия, таргетинг, минус-слова) — группы и объявления НЕ копируются (задокументировано в copy.py).
- **Коммит:** в home-репо, только 3 файла этой задачи.

## Сессия 2026-07-17 — Автоправила Фаза 2: backend Обзора и Сенсоров — ЗАДЕПЛОЕНО+верифицировано

- **Что сделано:** 6 сенсоров `direct/autorules/sensors/{balance,campaign_status,url_check,minus,goals,anomalies}.py` с интерфейсом `run(login, ctx)`. API-роуты `GET /api/ar/overview`, `/api/ar/balance`, `/api/ar/goals`, `/api/ar/filter-options`, `POST /api/ar/sensors/run` в `routes_autorules.py`. DI Victory conn + direct_tokens + blueprint_metrika в `autorules_main.py`. JS вкладок Обзор+Сенсоры в `autorules.html` (реальный fetch, рендер таблицы, тумблеры, кнопка «Запустить проверку»).
- **Тест porg-ro552oi2** (cartrade196.site, Екатеринбург, y-direct-victory): balance found=1 (0 ₽ < 500 ₽), campaign_status found=2 (MODERATION+OFF), goals found=1 (нет счётчика), anomalies found=0, overview DB = 3 строки с расходом. `direct-autorules.service` active, журнал чистый.
- **Фикс в процессе теста:** balance — v5 accounts.get пуст для клиент-логина → переключил на Live v4 AccountManagement.Get (как в account_service.py). goals — invalid FieldNames TextCampaign/SmartCampaign → используем TextCampaignFieldNames в extra-параметре + DB-fallback.
- **Осталось:** конверсии по выбранной цели в Обзоре (не реализованы — требуют Reports API или Метрику; отмечено в отчёте). Сенсор minus/url_check — не тестировался на аккаунте с живыми объявлениями.
- **Коммит:** 331025c.

## Сессия 2026-07-17 — Вынос API «Обучение ИИ» в `direct-ai.service` :5026 — КОД ГОТОВ, НЕ АКТИВИРОВАНО
- **Мотив:** роуты `/direct/api/ai/*` ходят в M3/LLM (долгие блокирующие вызовы) и занимали Flask-воркеров того же процесса, что создание РК. Отдельной страницы у вкладки нет — выносится ТОЛЬКО API (UI остаётся на :5020).
- **Новый `direct/ai_main.py`** (образец accounts_main/slepki_main): :5026, `DIRECT_ROLE=web` setdefault ДО импорта automation_runtime, domain wiring only (без direct.blueprint), 8 роутов, threaded=True. **`routes_ai.py` не менялся.**
- **`blueprint.py`** — флаг `DIRECT_REGISTER_AI` вокруг register_ai_routes (:347), дефолт "1" = обратная совместимость. Новый `deploy/direct-ai.service` + `deploy/dropins/direct-create.service.d/ai.conf` (DIRECT_REGISTER_AI=0).
- **Evidence:** md5 Mac==LXC101; py_compile OK (прод-венв 3.11, локальный 3.9 не тянет синтаксис); import ai_main → ровно 8 `/direct/api/ai/*` + только nav/static; флаг: unset→8, =0→0, =1→8; AST+identity сверка всех **21** deps с blueprint = IDENTICAL, порядок совпал.
- **⚠️ Находка (важная):** ai-путь читает env в момент вызова, а прод-значения ≠ дефолтам кода → в юнит добавлен паритет: `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` (код: v4-flash), `DIRECT_SITELINK_REUSE_ACCOUNT=1` (код: 0), `NEURO_PACK_MOUNT=/opt/neuro_content_local`, `DIRECT_CONTENT_REUSE_ACCOUNT=1`. Без них вкладка молча генерила бы иначе.
- **⚠️ Дрейф репо↔прод (не чинил):** на проде 3 drop-in'а direct-create НЕ в git — `flags.conf`, `reuse.conf`, `slepki.conf`. Пересборка по dropins/README поднимет прод без них → поедет генерация и вернутся slepki-роуты. Отдельной задачей.
- **НЕ активировано** (по границам): сервис не поднят, nginx не тронут, не коммичено. Инструкция активации (порядок шагов критичен: сначала :5026 + nginx, ПОТОМ ai.conf) + откат — в `.claude/sdd/direct-ai-service-report.md`.

## Сессия 2026-07-17 — ФАЗА 1 брокера доступа `direct-gateway.service` :5025 — ЗАДЕПЛОЕНО+верифицировано (greenfield)
- **Мотив (опция C Семёна):** каждый из 6 direct-процессов порознь держит куку в `campaign._ACCOUNT_COOKIE_CACHE` и независимо долбит главпоток. Брокер = ЕДИНСТВЕННЫЙ владелец кук/токенов/главпотока/units, один probe на всех.
- **ФАЗА 1 = ТОЛЬКО greenfield.** Существующие call-sites НЕ тронуты (campaign.py/yandex_gateway.py/automation_runtime.py целы). Миграция потребителей = Фаза 2 (отдельно).
- **Новый `direct/gateway_main.py`** (образец accounts_main): standalone Flask :5025, bind СТРОГО 127.0.0.1, `DIRECT_ROLE=web` (setdefault ДО import automation_runtime). БЕЗ blueprint/nav/auth (loopback-only). 8 эндпоинтов `/gw/*`: health, cookie, token, tokens, units_alive, resolve_agency, agency_override GET/POST. Оборачивает готовые `campaign.pick_working_cookie` + `yandex_gateway.{token_for_login,direct_tokens,units_alive_for_login,resolve_agency_hint,agency_override_get/save}`. DI берётся импортом automation_runtime.
- **Новый `direct/gateway_client.py`** — тонкий клиент для Фазы 2 (call-sites ещё НЕ переключены): `gw_cookie/gw_token/gw_tokens/gw_units_alive/gw_resolve_agency/gw_agency_override_get/save`. КАЖДАЯ: HTTP-таймаут 4с на GATEWAY_URL (default http://127.0.0.1:5025, fallback-URL из DIRECT_GATEWAY_HOST/PORT) → при ЛЮБОЙ ошибке ФОЛБЭК на локальную функцию (без него рестарт gateway ронял бы create).
- **systemd `/etc/systemd/system/direct-gateway.service`** (образец direct-accounts): DIRECT_ROLE=web, HOST=127.0.0.1 PORT=5025, NEURO_PACK_MOUNT, ExecStart `-m direct.gateway_main`, enable --now, active.
- **Evidence:** py_compile Mac+LXC101 OK; md5 Mac==LXC101 (`f0cb1a0e…` main, `9f9bf716…` client). :5025 bind 127.0.0.1 (НЕ 0.0.0.0), health `{ok:true}`, 1 тред (нет recover/worker/sweep в журнале). /gw/tokens=7 агентств с токенами; /gw/token(porg-usmc4253)=реальный токен y-direct-victory; /gw/cookie=1381 симв (live-probe главпотока сработал); units_alive=true; resolve/override GET/POST round-trip OK (temp-строка вычищена). Фолбэк доказан: битый порт :5999 → те же данные (7 tokens, cookie 1381) БЕЗ новых запросов к брокеру (журнал), live :5025 → GET /gw/* 200 в журнале. /gw НЕ в nginx; снаружи `https://seoadvanced.ru/gw/health`=404. Другие 6 direct-сервисов active, файлы не менялись.
- **Осталось:** Фаза 2 — переключить call-sites на gateway_client (отдельно). Приёмка direct_verifier. НЕ коммичено.

## Сессия 2026-07-17 — Вынос САМОЙ СТРАНИЦЫ дашбордов «Обзор»/«Статистика» в direct-accounts :5024 — ЗАДЕПЛОЕНО+верифицировано
- **Мотив (продолжение прошлой сессии):** API дашбордов уже был на :5024, но панели физически жили ВКЛАДКАМИ в index.html (:5020) → правка их HTML/JS требовала рестарта создания РК. Теперь страница вынесена.
- **Новая страница `templates/direct/accounts.html`** (образец slepki.html/copy.html): обе панели (panel-accounts + panel-stats) 1:1 из index.html + под-вкладки (клиентский `showAcctSub`). Роут `accounts_main.py` → `@bp.route("/automation/accounts")` render_template. DIRECT_ROLE=web (setdefault ДО импортов) цел.
- **`static/direct/accounts_ui.js`** (самодостаточный, 913 стр) — ВЕСЬ dashboard-JS + дублирует шаренные хелперы (esc/uiConfirm/setCurAccount/loadAccounts-ПОЛНАЯ/loadAgents/AGENTS_INFO/renderAgentHint/setTopMsg/progress). index.html его НЕ подключает (redeclare-конфликт). `accounts_ui.css` — копия dashboard-стилей da-* из inline `<style>` index.html + стили под-вкладок.
- **index.html усохла 6433→5519** (−913 стр): удалены обе панели + dashboard-only JS (таблица Обзор/фильтры/сортировка/открут/баланс/блокировки + весь блок Статистика + промо ИИ + мёртвый OV_*). Табы Обзор/Статистика → ССЫЛКИ на /direct/automation/accounts. `switchToPanel` без веток accounts/stats (+ null-guard в списке панелей). Дефолт-панель теперь `create` (switchToPanel на заходе без ?tab). `?tab=accounts|stats` → JS-редирект на новую страницу.
- **КЛЮЧЕВОЕ решение по сцепке:** `loadAccounts`/`ensureAllAccounts`/`ACC_ALL`, `loadAgents`/`AGENTS_INFO`/`renderAgentHint`, `setCurAccount`/`CUR_ACCOUNT`, `esc`/`setTopMsg`/`uiConfirm` реально нужны панели СОЗДАНИЯ РК → ОСТАЛИСЬ в index.html. Чистого «всё в один static для обеих» без риска create нет → сделал самодостаточный accounts_ui.js (как copy_common.js/пролог slepki.html). В index.html `loadAccounts` заменён на СЛИМ-версию (только фетч ACC_ALL для ulogin-подсказок, без рендера таблицы). `createForStatsAccount` на дашбордах → навигация `/direct/automation?tab=create&login=`.
- **nginx** `location ^~ /direct/automation/accounts → :5024` ДО общего `/direct/`. Бэкап `.bak.acctpage_20260717_142725`, nginx -t OK, reload.
- **Evidence:** md5 Mac==LXC101 (accounts_main/accounts.html/accounts_ui.js/.css/index.html все совпали); py_compile OK Mac+LXC101; node --check обоих JS OK; Jinja compile обоих шаблонов OK; **изоляция:** restart direct-accounts → create 15600/worker 5774 НЕ изменились; restart direct-create → accounts pid 20322 НЕ изменился, страница жива; journal :5024 (pid 20322) показал `GET /direct/automation/accounts 302` = nginx рулит на :5024; static js/css 200; create/copy/slepki/accounts все 302 (auth, не 404); 0 dangling-ссылок на удалённые функции в index.html; 0 create-only ссылок в accounts_ui.js; startup без трейсбеков.
- **Осталось:** live-визуал под сессией в браузере (обе под-вкладки Обзор/Статистика рендерятся, промо ИИ, сортировка, откруты/баланс) — на direct_verifier + ui_verifier. Минор: `createForStatsAccount` кросс-страница не автозаполняет login формы создания (deep-link ?login= не читается create-панелью); мёртвый `scheduleApplyFilters` оставлен (безвреден). НЕ коммичено.

## Сессия 2026-07-17 — Вынос API дашбордов «Обзор»/«Статистика» в direct-accounts.service :5024 — ЗАДЕПЛОЕНО+верифицировано
- **Мотив:** read-only дашборды бьют в Direct API (медленно, могут ВИСНУТЬ). Раньше их зависание/деплой сидели в одном процессе с созданием РК. Вынесены в свой процесс → изоляция.
- **Новый файл `direct/accounts_main.py`** (по образцу slepki_main): :5024, `DIRECT_ROLE=web` через setdefault ДО импорта automation_runtime (иначе роль all подняла бы воркеров создания). DI берётся из `automation_runtime` (он на импорте делает `account_service.configure` + blueprint_metrika + repository — вся проводка). Регистрирует `register_account_routes` + `register_overview_routes`.
- **nginx** `/etc/nginx/sites-enabled/seoadvanced.ru`: 5 exact-match (`location =`) блоков ПЕРЕД общим `location /direct/` → :5024: `/direct/api/{overview,account_stats,balance,accounts_otkrut,statuses}`. Бэкап `.bak.accounts_20260717_134620`. Exact-match критичен: `/direct/api/accounts` (пикер создания) НЕ ловится и остаётся на :5020.
- **Граница (перепроверена по index.html):** переносимые вызываются ТОЛЬКО дашбордами. `account_assets` используется И созданием (2269 «Обновить фиды») И дашбордом (5308 Статистика) → ОСТАЁТСЯ на :5020 целиком (в move-list его нет). На :5020 остались prefill/assets/audiences/goal_for_counter/account_info/accounts.
- **systemd** `/etc/systemd/system/direct-accounts.service` (образец direct-slepki): DIRECT_ROLE=web, DIRECT_ACCOUNTS_PORT=5024, enabled+active.
- **Evidence:** py_compile Mac+LXC101 OK (md5 `bfc5a87a…` совпал); nginx routing доказан журналами werkzeug (:5024 получил overview/account_stats/accounts_otkrut; :5020 получил account_prefill/accounts); restart direct-accounts → create pid 230/worker 5774 НЕ изменились; restart direct-create → :5024 жив, /overview отвечает; :5024 threads=1 (нет create-демонов); старт без трейсбеков.
- **Осталось:** live-проверка вкладок Обзор/Статистика в браузере под сессией (проводка доказана, curl без куки даёт 401) — на direct_verifier/Семёна. Не коммичено.

## Сессия 2026-07-17 — Копия porg-mushirne→porg-jh2si7rh: фикс organic/placementTypes/promoExtension

### Что сделано
- **organic/placementTypes (712850009)**: `platforms.organic=False, platforms.gallery=False` — исправлено живым зондом. Источник: `set_campaign_organic_and_placement` не патчил `biddingStategyWithPlatforms.platforms.organic/gallery`; фикс — патчить ОБА уровня (кампанейный флаг + стратегические платформы). Верифицировано зондом: organic True→False, pts [ADV_GALLERY,SEARCH_PAGE]→[SEARCH_PAGE], стратегия AUTOBUDGET→AUTOBUDGET.
- **promoExtension (код)**: в `direct_copy.py:phase_pull` удалены невалидные FieldNames "Status"/"State" из `promotions.get` → v5 error 8000 больше не будет → snapshot.promotions заполнится. `direct-copy.service` рестартован.
- **Три новых guard**: `OPTIMIZE_CONVERSIONS+avgCpa=None → AUTOBUDGET` (не AUTOBUDGET_AVG_CPA); `DEFAULT → _unsupported_strategy`; `MULTIPLE_CPA → _unsupported_strategy`.

### Что ЗАБЛОКИРОВАНО (3/5 кампаний без safe write-enum)
- 712850007 (DEFAULT/ручные ставки): нет write-enum для DEFAULT → skipped
- 712850008 (OPTIMIZE_CLICKS): нет write-enum → skipped
- 712850299 (MULTIPLE_CPA/тёплый спрос): MULTIPLE_CPA невалиден в write → skipped
- Для этих трёх: organic=True, pts=[ADV_GALLERY,SEARCH_PAGE] — невозможно исправить без обходного write-пути.

### Текущий diff (после фиксов)
- organic DIFF: 3/5 (только заблокированные)
- placementTypes DIFF: 3/5 (те же)
- promoExtension DIFF: 4/5 (требует нового copy run после рестарта сервиса)

## Сессия 2026-07-17 — CT0000_GROUPS + ТРИ АККАУНТА — ЗАВЕРШЕНО

### Фикс CT0000_GROUPS_FALLBACK_TO_SINGLE_TOVARNAYA (тройной)
- Root-cause: (1) `_struct_items` ~1624 пропускал ct0000 → `_items=[]`; (2) fallback ~864 создавал 1 «Товарная галерея»; (3) ранний выход `_tp1_pack_groups` ~738 срабатывал на пустом паке даже без сбоя M3.
- Фикс `create_set_tp1_builders.py`: (1) ранний-выход bypass — структурная проверка ct0000+gk для обоих путей tp1/tp5; (2) фолбэк-блок без гейта `_og is not None`; (3) skip-condition допускает ct0000+gk. Финальный md5 `0f16091d40a2850db07e5f2269522060` Mac==LXC101.

### Все три аккаунта ✅ верифицированы через v501 + Grid
- **avto_sk / porg-vfdnaolu**: 10 ЕПК-кампаний — Макс×2 (1гр «ЕПК макс»), Рет×4 (1гр «ЕПК рет»), 3гр×4 (3гр «Автотаргет»/«Рет»/«Все вместе (интересы)»). Grid: 18 кампаний. ✓
- **avtolajt_bu / porg-yzw6hkyk**: tp1 1 кампания 3 группы (Купить б/у авто, Кредит, Рассрочка); tp5 4 кампании×3 группы (Макс/Lul/Все); tp7 12 кампаний (3 типа×4 фида), ГОРОД→Краснодар. ✓
- **sk_krs / porg-usmc4253**: tp1 1 кампания **1 группа** («Товары — марка модель», id 5773945086 → ShoppingAd=1, ListingAd=1, TextAd=0, гео в RegionIds группы `[10995]`); tp7 **2 кампании** (`712851249` «Товарка - ТК · Марки и модели», `712851273` «Товарка - ТК · Рендеры», обе `source=UAC`/`metaType=ECOM`), всего в аккаунте 3 РК, все DRAFT. ✓ ⚠️ Исправлено 2026-07-17 (v5+Grid+UAC): раньше тут стояло «tp7 8 кампаний (2 типа×4 фида)» — по факту 2; и «2 группы (Краснодарский край, Товары — марка модель)» — «Краснодарский край» это суффикс ИМЕНИ кампании (`tp1_cpc_site — РСЯ - Модели - Автотаргетинг - Краснодарский край`), прошлая сессия распарсила его как отдельную группу. Регион в имени ≠ группа.
- Во всех трёх: ГОРОД нет, State=DRAFT, черновики.

### Осталось убрать
- Temp-файлы LXC101: probe_avto_sk.py, verify_tp5_groups.py, probe_avtolajt.py, probe_sk_krs.py, verify_avto_sk.py, verify_avtolajt.py, verify_avtolajt2.py, verify_grid_tp7.py, check_sk_krs.py, verify_sk_krs.py — можно удалить в любое время.

## Сессия 2026-07-16 — Унификация имён кампаний+групп ПРИМЕНЕНА (чистый срез), задача закрыта
- Источник: Google-таблица `1oGuvI…` — столбцы «финальное название» (кампании, Лист1) и «Новое (по шаблону)» (вкладка «Группы (кодер→имя)»).
- **Кампании: применено 980** без коллизий → 814 пар old→final, 6414 вхождений camp_names/tp6-7 `t`. Пропущено 3: kuderko smart-banner (нет в структуре) + 2 pavlov (создали бы новую коллизию).
- **Группы: применено 103** без коллизий (104 точных матча − 1 гард от новой коллизии; 21 chepelev tp2 = no-op, имя уже без кодера).
- Новых коллизий: **0** (дельта дублей 0 и по кампаниям, и по группам). Структура цела: 17 дир / 14617 групп / 14825 items. JSON валиден. md5 Mac==LXC101.
- Бэкапы: `slepki_structure.json.bak.names_apply_20260716_181907` (камп), `.bak.groups_apply_*` (группы).
- **Коллизийный остаток НЕ применяли** (решение Семёна «остаток закрываем»): 1119 кампаний (307 уник. дублей) + 41 группа + 3 edge — оставлены со старыми именами.
- Скрипты: scratchpad/read_sheet.py, analyze_sheet.py, apply_safety.py, apply_names.py, apply_groups*.py, verify_apply.py.

## Сессия 2026-07-16 — COPY минус-площадки: baseline-таблица + аудит интересов tp6/7
- **Регрессия copy (починена):** после per-слепок правки `_enabled_minus_places(slepok="")` при пустом slepok → `[]`. copy зовёт `enabled_minus_places()` без аргумента (`copy_steps.py:297`) → стал класть 0 площадок (тихо, `step_disabled_places` пропускает при пустом). Раньше copy читал глобальную таблицу.
- **Решение Семёна:** copy (клон 1:1, слепка нет) → ОТДЕЛЬНАЯ явная baseline-таблица `public.direct_baseline_minus_places`, сид = 122 URL из `direct_slepok_minus_places WHERE slepok='sk_krs'`.
- **Природа 122:** это НЕ бизнес-площадки sk_krs, а стандартный «мусорный РСЯ»-список (игры/VPN/чистилки/маркетплейсы: com.miui.cleaner, com.freevpnplanet, com.allgoritm.youla…). Универсален → глобальный baseline не нарушает «нельзя смешивать слепки».
- Движок: добавлены `_baseline_minus_places*` + `_enabled_baseline_minus_places()`; copy DI (`copy_engine.py` ~1397/1778) → baseline. create-путь НЕ тронут (per-слепок). **[in-progress: fixer + verify]**
- **⚠️ ИСПРАВЛЕНИЕ прошлой пометки:** «рестарт не нужен — статическая задача» (секция per-слепок ниже) БЫЛА НЕВЕРНОЙ — движковые правки требуют рестарта воркера (иначе стейл-модули → 8 ошибок `_enabled_minus_places() takes 0 positional`). Copy-джобы крутятся в **direct-copy.service** (:5022, in-process worker), не в direct-create-worker — фикс copy требует рестарта direct-copy.
- **Аудит tp6/7 интересы/аудитории по 7 слепкам (read-only, live-сверка):** гапы —
  - `scherbakova`: **40 ACTIVE tp7-кампаний с интересом n055 (Автокредит), ct0006 — ЗЕРО в структуре.** Крупнейший гап. + ct-паттерны tp7 (ct0001/0026/0044/0111/0181) в структуре отсутствуют.
  - `pavlov`: 2 живые tp7 interest-кампании (ТК-Интересы, ТК-Конкуренты-Интересы, Ставрополь) — в структуре нет. tp6 «Интересы» camp_names есть, но n000 (без ID).
  - `salamahin`: n-коды все n000 (совпадает с кабинетом, гапа нет), НО 35/77 tp6-кампаний имеют ретаргет/LAL audience-условия — структура не имеет поля audience_ids (системный гап всех слепков).
  - `piterkina`: tp6 Монобренд 3 «Интересы» camp_names, n000, кабинет не доступен агенту.
  - `kryuchkova`: интересов нет ни в структуре, ни в кабинете (все автотаргет) — чисто, гапа нет.
  - `karavaev`: интересов в структуре нет; по последнему харвесту в кабинете тоже нет.
  - `tumashenko`: [ожидается результат].
  - **Системный барьер:** конкретные goal_id/audience_id для interest-кампаний недоступны через API (UAC 403 на всех агентствах Victory, Grid не отдаёт поля targeting/retargetings). Нужен вход владельца в кабинет или скрин раздела «Интересы».

## Сессия 2026-07-16 — Per-слепок минус-площадки — КОД+БД+МИГРАЦИЯ, создание РК НА ПАУЗЕ
- Новая таблица Victory `public.direct_slepok_minus_places(slepok,url,enabled,sort,updated_at PK(slepok,url))`.
- Миграция: `direct_global_minus_places` очищена (была 122 строки), 122 URL загружены в `direct_slepok_minus_places` slepok='sk_krs'. Верифицировано SQL.
- Движок: `_enabled_minus_places(slepok="")` — принимает slepok, читает per-слепок. Без slepok возвращает []. Без глобального fallback.
- `create_set_tp1_builders.py` строки 1272, 1869: `_enabled_minus_places()` → `_enabled_minus_places(slepok)` (slepok — параметр обоих вызывающих функций).
- `routes_settings.py`: GET `/api/minus-places?slepok=` (обязательный параметр, 400 без), POST требует `slepok` в теле, пишет в `direct_slepok_minus_places`.
- `blueprint.py`: передаёт `slepok_minus_places=_slepok_minus_places, slepok_minus_places_ensure=_slepok_minus_places_ensure` в `register_settings_routes`.
- `index.html`: кнопка «Минус-площадки» в тулбаре «Структура слепков» + `slepkiOpenMinusPlaces()` (uses `_SL_SLEPOK`) + `_slMpSave()`. `loadMinusPlaces(slepok)` теперь принимает slepok: без аргумента рисует селектор слепков, с аргументом — грузит. `saveMinusPlaces` читает slepok из `data-mp-slepok` атрибута.
- py_compile OK (4 файла). SQL: sk_krs=122/global=0/terehov=0 проверено. НЕ деплоился (рестарт не нужен — статическая задача). НЕ верифицировалось live (создание на паузе).

## Сессия 2026-07-16 — ФИЧА «тег каталоги» — КОД ЗАДЕПЛОЕН, БД ЗАПОЛНЕНА, создание РК НА ПАУЗЕ
- Код: `create_set_structure.py` (+`CATALOG_TAG="каталоги"`, whitelist `detect_protected_tags`), `create_set_plan.py` (импорт `CATALOG_TAG as _CAT`, флаг `tp1_catalog=True` в `_emit_tp1`), `create_set_tp1.py` (строка ~80: `tp1_shopping = ... or bool(it.get("tp1_catalog"))`).
- БД: tag_registry id=7 label='каталоги' color=#f2a03d; campaign_tags: tp1=181, tp3=62, tp5=104, tp7=309, tp6=0 (guard OK).
- md5 Mac==LXC101 для всех трёх файлов. Сервисы direct-create+worker active.
- Для tp3/tp5/tp7 тег no-op (ListingAd всегда). tp1: тег → tp1_catalog=True → tp1_shopping=True → каталожные объявления.
- НЕ верифицировано live (создание на паузе). Catalog-role гейт (catalog_only=True) цел.

## Сессия 2026-07-16 — UI: панель «Что означает кодер» доступна не-админам — ЗАДЕПЛОЕНО
- Баг: `slepki_keywords` и `slepki_coder_components` имели `if not _admin(): 403` → не-админ при клике на группу получал ошибку "только администратор" вместо расшифровки кодера/таргетингов.
- Фикс: убраны admin-гейты из обоих GET read-only эндпоинтов (`routes_slepki_edit.py`, строки 326-327 и 418-419). `canKw` в `index.html` (строка 2115) — убран `IS_ADMIN &&` (не-админы тоже видят счётчик ключей).
- WRITE-эндпоинты (edit_keywords/edit_callouts/save_assets/etc.) и JS-кнопка «✏ Редактировать» + бейдж «админ» — admin-гейт сохранён.
- md5 Mac==LXC101: routes_slepki_edit.py `9e7b3d128f7ad63799db455b9cadbd53`, index.html `9a2b1f0954d2ca5f2a120968c1cf543a`. Сервисы direct-create+worker active. Smoke OK: keywords 200, coder_components 200, edit_keywords 403 для не-админа.

## Сессия 2026-07-16 — Lazy-load конденсация документации (выполнено)
- Применён lazy-load к 4 файлам: STATE.md / ERRORS_JOURNAL.md / DOD.md / README.md.
- STATE.md: 635→369 строк (сессии 07-12 и 07-11 → STATE_ARCHIVE.md).
- ERRORS_JOURNAL.md: 2590→2498 строк; создан ERRORS_JOURNAL_ARCHIVE.md (✅-таблица + разбор прогона 07-06 A-K).
- DOD.md: 961→856 строк; создан DOD_ARCHIVE.md (§5.b file:line карта dmp + §5.d каноны реконструкции).
- README.md: 641→572 строк; создан README_ARCHIVE.md (историч. разделы июня-2024).
- CLAUDE.md direct/: добавлены ссылки на все 3 новых архивных файла в таблицу навигации.
- ERRORS_JOURNAL / DOD / README не достигли 200-500 — обоснование в отчёте агента (весь контент активный 🟡).

## Сессия 2026-07-16 — ЗАДАЧА #34 («все фиды» tp5 + tp1-РСЯ) — ЗАДЕПЛОЕНО
- Реализовано потребление флагов `tp5_all_feeds` / `tp1_all_feeds` (ранее эмитировались, но игнорировались).
- Файлы: `create_set_feed_builders.py` (add `all_feeds` param to `_create_tp5_campaign`, `all_feeds_list` to `_create_tp5_single`), `create_set_gallery.py` (pass `all_feeds=bool(it.get("tp5_all_feeds"))`). tp1-РСЯ правки (`create_set_tp1.py`, `create_set_tp1_builders.py`) были сделаны в предыдущей части сессии.
- Механика: при флаге — ONE кампания (не fan-out), Phase 4a в `_build_tp1_adgroups` создаёт ОДНУ группу (ShoppingAd+ListingAd) на каждый разрешённый фид. tp7 не тронут. Default-path (all_feeds=False) без изменений.
- py_compile OK (все 4 файла), pyflakes: только pre-existing DI-globals. md5 Mac==LXC101 для всех 4 файлов. Сервисы `direct-create` + `direct-create-worker` active.
- Верификация live (реальный многофидовый аккаунт + прогон) — при следующем запуске с тегом «все фиды».

## Сессия 2026-07-16 — ЗАДАЧА #43 (tone-of-voice): 4 новых агента + 10 CROSS_SIGNATURE — задеплоено
- `ai_agents.py` md5 `f33dc7efbfe1226ea6b84a28a6a81a76` Mac==LXC101; `systemctl restart direct-create direct-create-worker` OK.
- Добавлено в AGENTS: piterkina (Lada/Tenet монобренд), avtolajt_bu (б/у Краснодар), avto_sk (б/у фид), sk_krs (мультибренд новых Краснодар).
- Добавлено в AGENT_ADS: piterkina (10 заголовков/5 текстов/4 сайтлинка), avtolajt_bu (10/3/4), avto_sk (0/1/0), sk_krs (0/1/4).
- CROSS_SIGNATURE: добавлено 10 записей (6 старых без сигнатуры: salamahin/gordeeva/zubakin/chepelev/tumashenko/kuderko + 4 новых); итого 15 ключей.
- build_titles_messages + build_campaign_messages: добавлена инструкция «≥2 фирменных фразы из корпуса».
- py_compile OK, pyflakes: 0 undefined-name (3 pre-existing f-string placeholder warnings не мои).
- Верификация тон-судьёй (≥50/60) — при следующем прогоне этих слепков.

## Сессия 2026-07-16 — ЗАДАЧА 7: content-fix #2 ПОДТВЕРЖДЁН на kryuchkova + КРИТ операц. уроки, грайнд стартует
Green light: fix #2 (create_set_assets.py md5 e58c4470) + инфра готова. Перевалидация kryuchkova Монобренд через сервис (job 359aff7926e3) = **CLEAN**: created 22/22, Fix#1 стоп-фраза «до 1XX% цены» = 0 hits (2350 строк), Fix#2 generic «Кредит и первый взнос 0» = 0, маркеры kryuchkova есть (выгода-45% ×219, срочность ×159). Регион Новосибирск (нет Волгограда). tp2/5: 159 ads, 0 img, 0 video (DoD 7.13 ✓). Флаг: catchphrases «распродажа/2 платежа» = 0 (ads-corpus gap в LLM-stream, не блокер).
- **🔑 КРИТ УРОК №1 — DIRECT_ROLE=web для прогонов.** Probe `create_app()` дефолт role=`all` → create_set_async кладёт джобу БЕЗ `_web_posted=true` → systemd-воркер (claim только `_web_posted='true'`, queue_server.py:1925) НИКОГДА не забирает → джоба вечно `queued` (ЭТО корень всех «стопоров», НЕ Victory/куки). Фикс: гонять `DIRECT_ROLE=web python3 -m direct._probe_task7 run ...`. Разовый анблок висящей джобы: `UPDATE ...jsonb_set(body,'{_web_posted}','true')`.
- **🔑 УРОК №2 — видео ОТЛОЖЕНО 180с (добивка).** Видео вынесено из create в delayed_content_repair (`_DELAYED_CONTENT_REPAIR_DELAY_SECONDS=180`, campaign_spec_audit:874). Сразу после `done` tp1 hasVideo=0 — НОРМА; через ~3+ мин добивается (наблюдал 0→16, растёт). Проверять видео ПОСЛЕ задержки, иначе ложный VIDEO_MISSING.
- **🔑 УРОК №3 — WRONG_AUTOTARGET на свежих tp5 = лаг-ложняк.** live_verification даёт WRONG_AUTOTARGET на свежих tp5 (edit-view лаг реплики), а живой конфиг корректен (перечитал 27/27 = active/EXACT_V2_MARK/WITHOUT_BRAND). Перечитывать relevance_match до вердикта; edit-view keyword_count тоже лагает (0 vs showConditions 1389).
- **🔑 УРОК №4 — SSH:** локальное имя `lxc101` флапает (255) → юзать `lxc101-ts` (Tailscale). `pkill -f <pattern>` в ssh убивает СВОЙ шелл если pattern в его cmdline → бить по PID (`ps|grep|awk|kill`). Victory RO по умолч. (`B._victory_conn`/`victory_conn`); запись — `victory_conn_rw`.
- **СЛЕДУЮЩЕЕ:** грайнд всех слепков (pavlov→karavaev→gordeeva→salamahin→zubakin→chepelev→tumashenko→kuderko→piterkina→avto_sk→avtolajt_bu→sk_krs→terehov→scherbakova), delete_drafts на смене слепка, все site_type/tp 1:1, 1 фид, без cpa, DIRECT_ROLE=web. Stop-on-defect по стоп-фразе. Чекпоинты координатору.

## Сессия 2026-07-16 — ЗАДАЧА 7 ПОЛНЫЙ ПРОГОН: ПРЕРВАНО (fix-first по стоп-фразе) + 2 внешних отказа инфры
Автономный полный прогон по всем слепкам на porg-asfbs7qe. Прервано координатором: стоп-фраза системная → сначала фикс копирайтером (tp1 автотаргет brand-title путь мимо `_bad_ad_title`), потом продолжаем.
- **kryuchkova Монобренд:** создано 20/22 (2 потеряны на reconcile: после стопора воркера `_requeue_missing_positions_once` дедупит по ЖИВЫМ именам кабинета, а site_type'ы kryuchkova делят generic-имена с С пробегом → 2 item'а сматчились как «уже есть»). Регион Новосибирск ✓ (нет Волгограда), тон kryuchkova ✓, инварианты OFF ✓. **ДЕФЕКТ (системный, draft-only):** стоп-фраза «трейд-ин. До 150% цены авто» в tp1-автотаргет титрах (712819362 Марки / 712819381 Модели / 712819410 Марка) + тавтология body «Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽…». Фильтр `text_norm._bad_ad_title` РАЗВЁРНУТ и ловит строку (True), но tp1-путь пишет DeepSeek-титры в Grid МИМО фильтра. Фраза НЕ из seed/pack kryuchkova (runtime-LLM). kryuchkova Мультибренд НЕ запускался.
- **pavlov Мультибренд:** джоба `0b3cf75c94fb` (total=29) поставлена в очередь 00:51, но 2 часа висела `queued`, воркер НЕ забрал → probe TIMEOUT 02:52, создано 0. **Джоба-мина:** при восстановлении Victory воркер её заберёт → повесил watcher `_tmp_cancel_pavlov_watch.py` (nohup, отменит `0b3cf75c94fb` как только Victory отзовётся). Отменить ДО любого resume.
- **2 ВНЕШНИХ ОТКАЗА (не код):** (1) Victory DB 103.88.240.90:5432 — Connection refused (воркер fail-open sweep, джобы не берёт). (2) Кука porg-asfbs7qe — мёртвая сессия «need_reset / Истёк срок» (force_refresh не помог; нужен релогин агентства в главпотоке). Grid-список/удаление недоступны.
- **Аккаунт ЧИСТ:** v5 `campaigns.get` (агентский токен, без куки) = 0 text/unified. Клин 00:51 удалил все 30 kryuchkova-черновиков (by_v5=27, by_uac=3), pavlov создал 0 → UAC=0. Итого 0 кампаний.
- **STAND BY.** Ничего не создаётся (probe мёртв, воркер не берёт джобы). Ждём: фикс стоп-фразы задеплоен+repro-verified → перепроверка kryuchkova → полный прогон (Терехов/Щербакова последними, delete_drafts на смене слепка, 1 фид, без cpa). Порядок слепков и inventory — в отчёте сессии. ⚠️ `lxc101` (локальное имя) флапает — юзать `lxc101-ts` (Tailscale).

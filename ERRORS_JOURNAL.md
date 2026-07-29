# 📒 Журнал ошибок создания кампаний — нейродиректолог

> **Назначение:** каждая повторяющаяся ошибка создания РК фиксируется здесь: сигнатура → root-cause →
> метод решения → **помогло или нет** (проверено живым прогоном). Перед фиксом любой ошибки —
> СНАЧАЛА искать её здесь: возможно, решение уже известно или уже пробовали и не помогло.

### COPY_UAC_HREF_IMAGES_NOT_1TO1 — UAC-копия теряла paths и порядок картинок (2026-07-29)
- Симптом: copy job `5dc4ca62df05` (`porg-qrriv2wt` → `porg-63s3kxux`, target
  `geelybase-196.ru`) создала 12/12 tp6/tp7 кампаний, но ссылки `/quiz` и модельные URL частично
  стали главной страницей, а картинки не совпали 1в1. Live read-back: `hashes_equal=False` у всех
  12 пар; пример `712846924 /quiz` → `713137113 /`, `712847305 /auto/geely/monjaro/...` → `/`.
- Где: `copy_engine._copy_run_job` UAC-only branch + `copy_uac._copy_uac_campaigns`.
- Root-cause: UAC ветка не использует v5 snapshot (в job campaigns/adgroups/ads/adimages = 0) и
  строит `MasterCampaignSpec` сама. До фикса `href=target_href`, где `target_href` заранее был
  `https://<target_domain>` без исходного path. Картинки брались через `_copy_uac_media_urls` —
  рекурсивный поиск URL по нестабильному detail payload, включая preview/meta URL, без опоры на
  упорядоченный список `contents`.
- Решение: для каждой UAC-кампании брать исходный `d.href` и прогонять через `_copy_target_href`
  (замена домена с сохранением path/query/fragment). Для media недостаточно повторно загрузить
  `contents[].source_url`: UAC может пересоздать другой `direct_image_hash`. Финальный фикс —
  предварительно добавить исходные `AdImageHash` в target через v501 `adimages.add` из source
  `OriginalUrl`, затем создавать/патчить UAC через source `contents[].id`; URL-загрузка оставлена
  только fallback'ом, когда source content ids недоступны.
- Проверено кодом: `py_compile copy_uac.py`; pytest guard на `/quiz`, модельный path при пустом
  source_domain, ordered `contents[].source_url`, extraction `contents[].id/direct_image_hash` —
  5 passed.
- Repair job `5dc4ca62df05` (2026-07-29): созданы новые DRAFT `713139080, 713139079,
  713139089, 713139094, 713139101, 713139108, 713139120, 713139122, 713139140, 713139139,
  713139152, 713139153`; source content ids пропатчены в target. Для target-библиотеки пришлось
  добавить 3 missing hash: `AjTquKL-wjdC_BqHlXA0Mw`, `My3VJ0dS3PT3AKzJDXsTiw`,
  `g1nzRR5SFQe1H6CmN_mG6A`. Live read-back: все 12 `href_ok=True`, `img_ok=True`, status `draft`.
  Старые ошибочные DRAFT `713137051, 713137050, 713137061, 713137085, 713137087, 713137100,
  713137098, 713137113, 713137119, 713137124, 713137133, 713137136` удалены; повторный read-back:
  `old_gone_count=12`. Mapping записан в `direct_automation_jobs.result.manual_uac_repair_20260729`.
- НЕ помогло ранее: общий v5 `copy_verify` — в UAC-only job он видел 0 сущностей и не сравнивал
  href/images; cookie postprocess проверял только минимальное число картинок, не identity/order.
  Первый фикс через `contents[].source_url` починил href, но не гарантировал картинки: для model
  `get-uac/.../thumb|s4x3` Direct выдавал новые hash.

### CONTENT_EDITOR_AD_HREF_COOKIE_RIGHTS_MASKED — `ad_href` падал с ложным последним `401 No rights` (2026-07-29)
- Симптом: Agent Board #50 / `content_jobs.job_id=ce_39f8cdd30779` (`gordeeva`,
  `porg-nxhtsz6c`, `ad_href`, `/auto/changan/uni-s-cs55plus/i-restyling/suv-5d` →
  `/auto/changan/uni-s-cs55plus/1-rest/suv-5d`) упала как
  `ни одна кука не подошла ... HTTP 401 No rights`.
- Root-cause: управляющее агентство известно и v5-владелец подтверждён как
  `victoryagency-direct1618440`, но его fresh/local cookie на `linkinfo` возвращают
  `403 Нет прав`; дальнейший перебор чужих агентств возвращал `401 No rights` и затирал
  первичную actionable причину. `gateway_client.gw_cookie` после JSON-ошибки broker'а мог уходить
  в локальный fallback и повторять/маскировать тот же подбор.
- Решение: `campaign._pick_working_cookie_local` сохраняет ошибку управляющего агентства и в конце
  отдаёт `кука управляющего агентства ... не имеет web/Grid-прав`; `gateway_client.gw_cookie`
  распознаёт terminal cookie-rights ошибки broker'а и не делает локальный fallback.
- Live-проверка: `/gw/cookie?login=porg-nxhtsz6c&force_refresh=0` отдаёт новую ошибку по
  `victoryagency-direct1618440`; v5 read-back: 13 кампаний / 4362 ads, старый path в 26
  `RESPONSIVE_AD`, новый path = 0. No-op `ads.update` по одному ad_id вернул `3000 / Нет доступа к API`.
- Статус: 🟡 код задеплоен и сервисы `direct-gateway`, `direct-content`, `direct-content-worker`
  перезапущены; исходная операция не добита, нужен web/Grid-доступ к `porg-nxhtsz6c` или отдельное
  решение Семёна по способу правки.
- Повтор #51 (`ce_0970ceaea695`, `gordeeva`, `porg-m6atla56`): исходно упала тем же
  `ни одна кука не подошла ... HTTP 401 No rights`. Live root-cause 2026-07-29:
  v5-владелец/row agency/override = `victoryagency-direct1618440`; broker `/gw/cookie` после
  restart отдаёт точную ошибку `кука управляющего агентства ... не имеет web/Grid-прав` с
  `fresh/local linkinfo -> 403 Нет прав`. Доп. hardening: standalone/local single-account fallback
  без DI-resolver теперь тоже сохраняет эту terminal rights-ошибку, а не деградирует в generic
  `ни одна кука...`. Live Direct read-back: 20 `ResponsiveAd` в `OFF` кампаниях
  `702891187/702891201/702891211/702891248/702891267` всё ещё имеют старый path
  `/auto/changan/uni-s-cs55plus/i-restyling/suv-5d`, новый path = 0. Current Grid RMW execute
  остановлен до мутации на `403 Нет прав`; no-op official `ads.update` v5 и v501 по
  `ad_id=17256545488` вернул `3000 Аккаунт пользователя блокирован / Нет доступа к API`.
  Job row error обновлён на точную причину; добивка невозможна до возврата web/Grid-прав или
  отдельного решения Семёна по способу правки.
- Повтор #52 (`ce_f10156cabe2a`, `gordeeva`, `porg-q6m3wzlz`): исходно упала тем же
  `ни одна кука не подошла ... HTTP 401 No rights`. Live root-cause 2026-07-29:
  row agency/v5-владелец = `victoryagency-direct1618440`; текущий executor на fresh/local cookie
  останавливается до мутации с `403 Нет прав`. Live Direct read-back: 20 `TextAd` в DRAFT/OFF
  кампаниях `703013688/703013701/703013707/703013734/703013753` всё ещё имеют старый path
  `/auto/changan/uni-s-cs55plus/i-restyling/suv-5d`, новый path = 0. No-op official `ads.update`
  по `ad_id=17266309206` вернул `3000 Нет доступа к API / Аккаунт пользователя блокирован`.
  Job row error обновлён на точную причину; добивка невозможна до возврата web/Grid-прав или
  отдельного решения Семёна по способу правки.
- Повтор #53 (`ce_72ffaeb95c6d`, `gordeeva`, `porg-whs6d5n5`): исходно упала тем же
  `ни одна кука не подошла ... HTTP 401 No rights`. Live root-cause 2026-07-29:
  row agency и OAuth read-owner = `victorylotsofads1`; его fresh/local cookie на `linkinfo`
  возвращают `403 Нет прав`, а no-op official `ads.update` по `ad_id=17447602599` возвращает
  `3000 Нет доступа к API / Аккаунт пользователя блокирован`. Доп. hardening: executor
  `content_jobs` теперь создаёт Grid factory с явной `job.agency`, берёт cookie через
  `pick_working_cookie(login, accounts=(job.agency,), force_refresh=True)` и передаёт её в
  `get_grid_client`, поэтому job без resolvable managing-agency больше не деградирует в
  default-перебор с последней чужой `401`. Live Direct read-back: 13 неархивных `TextAd`
  в `OFF/DRAFT` кампаниях `705293824/705293850/705293872` всё ещё имеют старый path
  `/auto/changan/uni-s-cs55plus/i-restyling/suv-5d`, новый path = 0; ещё 10 old-path `TextAd`
  находятся в архивных кампаниях и не трогались. Row `content_jobs.error/result` обновлён на
  точную terminal-причину; добивка невозможна до возврата web/Grid-прав к `porg-whs6d5n5` или
  отдельного решения Семёна по способу правки.

### CONTENT_EDITOR_AD_HREF_API_WRITE_BLOCKED — `ad_href` падал на `ads.update: Нет доступа к API` (2026-07-29)
- Симптом (Agent Board #47, job `ce_2eb3812dd1c7`, `porg-qv22znqh`): смена
  `/auto/changan/cs75-plus/i/suv-5d` → `/auto/changan/cs75-plus/iv/suv-5d` нашла 54 цели,
  но завершилась `replaced=0`, `confirmed=0`, `ads.update: Нет доступа к API`.
- Где: `content_replace_routes.py::_replace_ad_href`.
- Root-cause: `ad_href` для `TextAd` писал через официальный v5 `ads.update`, а на этом клиенте
  write API возвращает `error_code=3000`, `Аккаунт пользователя блокирован`. Repro:
  no-op `ads.update` того же Href на `ad_id=17327384320` → `Нет доступа к API`; `ads.get`
  при этом читается нормально. Это противоречило текущему контракту content-editor: массовые
  записи делать через cookie/Grid.
- Решение: `ad_href` переведён на cookie/Grid RMW: `TextAd` → `text_ads_for_update` +
  `update_text_ads(..., allow_empty_image_hashes=True)`, `ResponsiveAd` →
  `adaptive_ads_for_update` + `update_adaptive_text_ads`; v5 остаётся только для read-back.
- Статус: 🟡 код проверен локально (`py_compile`, `direct/tests/test_content_ad_href_grid.py` —
  2 passed), `direct-content-worker` перезапущен. Повтор 2026-07-29 после команды админа
  "взять новую с главпотока": `/opt/scripts/.secret/glavpotok_cookies.py` обновил 6 свежих
  агентских cookie, включая `victoryagency-direct1618440`; `need_reset` ушёл, но Grid/UAC probe
  для `porg-qv22znqh` теперь возвращает `HTTP 403 / Нет прав`. OAuth read видит аккаунт только
  через `victoryagency-direct1618440`, остальные агентские токены получают `8800 Объект не найден`
  (или неагентский `8000`). Live `ads.get` по `17327384320`–`17327384329` подтвердил, что старый
  path всё ещё стоит в `OFF/DRAFT` кампании `703748303`; операция не добита до возврата web/Grid
  прав или отдельного решения по удалённому в реестре аккаунту.
- Follow-up по вопросу Семёна "куки пробовал перебором" (2026-07-29): да, выполнен полный live
  перебор cookie. Локальный `/opt/scripts/.secret/cookies.json`: 7 аккаунтов (`skuderko1` + 6
  default); свежий главпоток: 6 default-агентств, `skuderko1` отсутствует. Итог: управляющая
  `victoryagency-direct1618440` стабильно возвращает `403 Нет прав`, все остальные cookie —
  `401 No rights`; рабочей web/Grid cookie нет. Row `ce_2eb3812dd1c7` обновлён на
  `blocked_reason=cookie_rights_full_bruteforce_failed`. Live v5 read-back по 54 id: 12 `TextAd`
  всё ещё на старом path в `OFF/DRAFT` кампании `703748303`, новый path = 0; остальные 42 id уже
  ведут на другие модели и не являются текущими old-path целями.
- Повтор #48 (`ce_b27147271334`, `gordeeva`, `e-20074375`): job была исполнена старым worker
  `python-postgresql:3399472` до рестарта и упала тем же v5-путём. Repro 2026-07-29:
  no-op `ads.update` по `ad_id=17497079160` → `error_code=3000`,
  `Аккаунт пользователя блокирован / Нет доступа к API`; v5 read-back видит 12 активных `TextAd`
  в кампании `705858919` со старым path. Повторный прогон текущим Grid-кодом остановлен на
  cookie-доступе: `e-20074375` через `direct-gateway`/главпоток возвращает `need_reset`/Passport,
  поэтому live-добивка невозможна до перелогина агентской cookie. Job оставлена terminal `error`.
- Повтор #49 (`ce_7986eedfae67`, `gordeeva`, `e-20074377`): job была исполнена старым worker
  `python-postgresql:3399472` до фикса/restart и упала тем же v5-путём. Live read-back 2026-07-29:
  12 `TextAd` (`17495459782`–`17495459793`) в кампании `705838023` всё ещё имеют старый path
  `/auto/changan/cs75-plus/i/suv-5d`, новый path = 0. Доп. hardening: для `ad_href` отключён
  лишний `_load_account` callout/Grid enrichment (`include_callouts=False` в worker `/links`/
  preview/replace), чтобы чтение ссылок не зависело от cookie. Live-добивка текущим Grid-кодом
  невозможна сейчас: `direct-gateway` для `e-20074377` отдаёт 502/`need_reset`, фолбэк
  `glavpotok.ru` висит на SOCKS/SSL timeout; операция не применена, job оставлена terminal `error`
  до перелогина агентской cookie.
- НЕ помогло ранее: v5/v501 `ads.update` для Href (работало на части аккаунтов, но падает на
  аккаунтах с заблокированным official write API).

### COPY_ADS_GET_1000_TRANSIENT — ads.get падал с кодом 1000 и не ретраился (2026-07-28)
- Симптом (copy job `c3cb103f420a`, `porg-mjyh6hjv` → `porg-xqsyuplp`): после pull кампаний
  (`26` в скоупе) и групп (`1025`) джоба упала terminal `error` на
  `ads.get: [ads.get] 1000: Сервис временно недоступен`.
- Где: `/opt/scripts/work/slepki_direktologov/scripts/direct_copy.py:direct_call`.
- Root-cause: Direct API вернул временную недоступность как JSON business-error с HTTP 200.
  Общий retry-хелпер ретраил HTTP 5xx/429 и коды 52/506, но не 1000/1001/1002, поэтому
  `get_paginated()` сразу поднимал `RuntimeError` и `phase_pull` не завершал snapshot.
- Решение: добавить 1000/1001/1002 в `TRANSIENT_API_CODES`, чтобы все v5/v501 вызовы copy-path
  ретраили это окно тем же backoff, что и прочие transient-ошибки. Дополнительно `copy_main.py`
  запускает copy worker/retry-daemon на старте сервиса, иначе Agent Board `done` мог ждать
  следующего ручного `copy_start`.
- Проверено: synthetic repro до фикса = 1 `ads.get` и `__error__`; после фикса = 2 вызова и успех.
  `py_compile` OK, `direct/tests/test_direct_copy_transient_retry.py` — 1 passed. `direct-copy.service`
  active; Agent Board #46 `done` создал retry job `0c1ba3db2827` через штатный daemon.
- Статус: ✅ код задеплоен; retry job пошла обычной очередью.
- НЕ помогло ранее: точечный retry только для `campaigns.add` 1000 — не покрывал pull/read-вызовы.

### VIDEO_BRAND_FALLBACK_BEATS_EXACT_MODEL — подмена бренда из общего пула перебивала точный ролик модели (2026-07-28)
- Симптом: после переноса батча `by_code` в пак Павлова опустели папки общего пула `ct0118` (Haval H9), `ct0119` (Haval Jolion), `ct0120` (Haval M6) → чужой слепок получал `ct0112_01/02` (Haval **Dargo**), хотя точный ролик своей модели лежит в паке Павлова и достижим ступенью «чужой слепок». Замер: `ct0119` чужой слепок ДО `Haval_Jolion_1x1/_9x16` → ПОСЛЕ переноса `ct0112_01/02`.
- Где: `kontent_pack.py::videos_for_ct` — три ступени «свой слепок → общий пул → чужой слепок», а brand-fallback сидел ВНУТРИ ступени 2 (`videos_pool_for_ct`, хвост функции) → подмена бренда срабатывала раньше ступени 3.
- Root-cause: приоритет источника (пул) стоял выше точности совпадения модели. Для 17 остальных опустевших ct (Geely/Hyundai/Lada/Tank) brand-fallback замены не находил, поэтому ступень 3 отрабатывала и дефект был не виден.
- Решение (2026-07-28, решение Семёна «точный ролик Jolion важнее чужого Dargo»): `videos_for_ct` разложен на ДВА ЯРУСА с одним и тем же порядком источников: ЯРУС 1 — точная модель (свой слепок → общий пул → чужой слепок), ЯРУС 2 — подмена бренда (свой слепок → общий пул → чужой слепок). `videos_pool_for_ct(..., allow_brand=False)` — новый флаг «только точный ct» (дефолт `True`, все внешние вызовы не менялись). Внутри ступени слепка brand-подмена по-прежнему разрешена ТОЛЬКО брендовому ct (`allow_brand` от `feeds_ct_model`), но теперь тоже во втором ярусе.
- Проверено (LXC101, read-only, `NEURO_PACK_MOUNT=/opt/neuro_content_local`): OLD vs NEW по 38 ct × 3 кейса → 7 diff, ВСЕ одного класса «`ct0112_01/02` (Dargo) → точная модель»; по своему слепку `pavlov` diff=0. `ct0119` чужой слепок ПОСЛЕ = `Jolion.mp4, Jolion_1.mp4`; `ct0111` (точной модели нет нигде) остался `ct0112_01/02`; `ct0113` (канон в пуле цел) остался `ct0113_01/02`. `direct/tests/test_video_source_order.py` — 10 passed.
- ⚠️ Нюанс: среди ЧУЖИХ слепков папки перебираются по алфавиту (`_slepok_video_folders` → `sorted`), поэтому точный Jolion берётся из `haval_ufa_si7rw3ua` (`Jolion.mp4`), а не из `pavlov` (`Haval_Jolion_16x9.mp4`) — оба точные, порядок папок этой правкой не менялся.
- Статус: 🟡 код на Mac+LXC101 (Mutagen, md5 совпал), сервисы НЕ рестартовались — ждёт живого прогона.
- НЕ помогло ранее: — (первая правка приоритета ярусов). Смежные: `VIDEO_BRAND_FALLBACK_DEPENDS_ON_FEED_IMAGE` (расширение brand-fallback, не приоритет), `VIDEO_NO_POOL` (нет роликов физически — не баг).

### JOB_GREEN_DESPITE_LOSSES — потери прогона не влияли на итог джобы (2026-07-28)
- Симптом: аудитории не отправлены (пустой `ret_map`), минус-фразы не влезли (`not_packed`), набор в кабинете разошёлся со слепком, наборы привязались не ко всем кампаниям — а карточка джобы остаётся **зелёной** (`✅ готово`), и в API джоб этих фактов нет вовсе.
- Где: все такие факты уходили ТОЛЬКО в `_add_job_err` → `queue_server.py:206 job["errors_log"]`. Но `create_job_status.compute_job_issues_breakdown` прямым текстом относит `errors_log` к «NOT significant alone» и считает `has_issues` исключительно из `live_verification.summary.errors` + `verification.summary.errors`; в `/direct/api/create/jobs` (`routes_jobs.py`) поля `errors_log` нет; в `*.js`/`*.html` — 0 вхождений. Комментарий в оркестраторе при этом утверждал, что джоба «обязана быть НЕ зелёной» — ложное утверждение, снято.
- Root-cause: верификаторы сверяют СОЗДАННОЕ, а не ОТПРАВЛЕННОЕ. То, что не уехало вовсе, для них невидимо по построению, а второго канала значимости не было.
- Решение (2026-07-28, вариант «а» — без второго UI-канала): оркестратор ведёт `_losses` через единый `_note_loss(kind, message, count)` (он же по-прежнему пишет в `errors_log`), список едет в `result["losses"]` (`create_set_response.build_create_set_response`), а `compute_job_issues_breakdown` считает непустой `losses` ЗНАЧИМЫМ наравне с ошибками верификации и добавляет ключ `losses` в разбивку. Точки потерь: `audiences_not_sent`, `minus_sets` (все `res["errors"]` наборов, включая `not_packed`/mismatch), `minus_sets_not_attached`, `minus_sets_attach_failed`, `minus_sets_partial`, `minus_sets_exception`. Карточка джобы (`automation_jobs.js`) печатает «потерь=N» и уходит с `✅` на `⚠️`.
- ⚠️ `annotate_job_issues`: `elif` → отдельный `if`, иначе потери при отсутствующей верификации затирали бы честный `has_issues_unknown`. Для старых путей поведение идентично (разбивка непуста ⟹ `*.summary` есть ⟹ данные верификации есть).
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_minus_losses_round3.py` + `test_minus_audiences_round2.py` + `test_job_status_gate.py` + `test_second_token_thread.py` + `test_grid_add_idempotency.py` + `test_create_review_findings.py` + `test_create_auto_regressions.py` — 232 passed.
- ⚠️ Не переизобретать: (1) подмешивать потери в `verification.summary.errors` — это враньё в отчёте верификатора (там число НАЙДЕННЫХ им дефектов); (2) заводить второй канал видимости через отдачу `errors_log` в API джоб — гейт `has_issues` уже работает и проверен.
- НЕ помогло ранее: писать потери только в `errors_log` (раунды 1-2 по MINUS_SETS_NEVER_APPLIED и TP1_TP5_STRUCT_AUDIENCES_NEVER_APPLIED) — журнал джобы на её цвет и на API не влияет.

### MINUS_SETS_NEVER_APPLIED — именованные наборы минус-фраз слепка не доезжали в кабинет (2026-07-28)
- Симптом: `porg-xjxpfxby` (слепок `kuderko`, «С пробегом») — в структуре 4 именованных набора / 1635 уникальных фраз, в кабинете доехало 107, НЕ доехало **1528**; наборов в библиотеке минус-фраз аккаунта — **0**. Тот же ноль на `porg-rgwzgo57`/`porg-nqavjicg`/`porg-dmwfp3dk`.
- Где: `_minus_sets/{slepok}.json` (пишет `slepki_editor.apply_save_minus_sets`) имел ЕДИНСТВЕННОГО читателя — `slepki_editor.read_minus_sets` (UI). В движке создания вхождений не было вообще (`rg "_minus_sets"` по `create_set_*.py` = 0).
- Root-cause: механизма не существовало. Смежный `_SLEPOK_MINUS_MODE` (`create_set_minus.py`) не содержал `kuderko` → молчаливый дефолт `group`, но это НЕ причина: у kuderko `{slug}_minus_shared.txt` = 0 файлов и ct-уровневый `{slug}_minus.txt` в tp2 = 0 файлов, поэтому `_collect_pack_minus` даёт 0 фраз в ЛЮБОМ режиме (per-group слой поднимается только ПЕРЕСЕЧЕНИЕМ, а 118 per-group файлов разнородны → пересечение пусто).
- Решение (2026-07-28, решение Семёна «набор минус фраз должен добавляться в библиотеку минус фраз и потом добавляться в кампании тп2-тп5»): `create_set_minus.py` — `_read_slepok_minus_sets` (читает `{PACK_ROOT}/{site_type}/_minus_sets/{slug}.json`, НЕ зависит от `_minus_shared`), `ensure_named_minus_sets` (v5 `negativekeywordsharedsets.get` → переиспользовать набор с ТЕМ ЖЕ именем → `add` только недостающих), `ensure_named_minus_sets_cached` (lock+кэш на `(login,slepok,site_type)`: два токен-потока не должны создать дубль). `create_set_orchestrator.py` — НЕгейтованный блок после batch-аспектов: `select_campaign_ids_by_tp(created, (2,3,4,5))` → `apply_minus_sets_batch` (narrow-RMW merge `libraryMinusKeywordsIds`, уже привязанное сохраняется). tp1 намеренно не в списке (см. TP1_CAMPAIGN_MINUS_WRONG).
- Лимиты Директа (офиц. справка `.claude/skills/yandex-direct/docs/keywords/negative-keywords-library.md`): **30** наборов на аккаунт (:12), **4096** символов без пробелов на набор (:22/:28/:30), **3** набора на одну кампанию (:12/:41), ≤7 слов во фразе (`docs/keywords/negative-keywords.md`). Превышение → набор НЕ создаётся + видимая ошибка в `errors_log` джобы; тихой обрезки нет. Замер каноничных наборов kuderko: 4020 / 1586 / 2415 / 4045 симв. — каждый влезает в 4096, но 4 набора > лимита 3 на кампанию → при отказе привязки идёт ретрай первыми 3 + громкая ошибка со списком непривязанных id.
- Детект: `.claude/sdd/detect_minus_sets_gap.py` (read-only) — `MISSING` и `lib_sets` по паре (логин, слепок). Было `porg-xjxpfxby` MISSING **1528** из 1635, `lib_sets=0`. Ожидание после прогона: MISSING → 0, `lib_sets ≥ 4`, групповые минуса не уменьшились (331 группа из 720), `ExcludedSites=135` на tp1 на месте.
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_minus_sets_library.py` — 15 passed; полный `direct/tests` (без 3 файлов, требующих БД) — 5 failed / 616 passed, тот же набор падений, что и до правки (чужие правки в работе).
- ⚠️ Не переизобретать: (1) слепой фолбэк на первый набор аккаунта (`msets[0][0]`) — запрещён, матчим ТОЛЬКО по точному имени набора (иначе к нашей кампании прицепится ЧУЖОЙ набор директолога, см. PACK_MINUS_PER_GROUP_LOST); (2) поднимать per-group минуса ОБЪЕДИНЕНИЕМ, чтобы «добрать фраз», — опровергнуто замером там же (94 % своих ключей под нож); (3) чинить это сменой `_SLEPOK_MINUS_MODE` у kuderko на `shared_set`/`campaign` — источников кампанийного уровня у него нет физически, а групповые минуса (331/720) при этом СНЯЛИСЬ бы.
- НЕ помогло ранее: (первый фикс по этой сигнатуре). Смежные: `PACK_MINUS_PER_GROUP_LOST` (per-group слой, другой источник), `#9 SLEPOK_MINUS_MISSING_ONLY_GLOBAL` (только `_minus_shared`), `TP1_CAMPAIGN_MINUS_WRONG` (почему tp1 без минусов).
- **Раунд 2 (2026-07-28, ревью коммита `dda2ec09`, 🟡 ждёт прогона):** первый фикс создавал ВСЕ наборы структуры, а Директ разрешает ≤3 на кампанию → у `kuderko/«С пробегом»` 4-й набор «Марки и модели авто» (685 фраз = 41 % объёма) отваливался; у `karavaev/Мультибренд` не привязались бы 2 из 5.
  - Решение Семёна «объединять в 3 набора»: `create_set_minus._pack_minus_sets` — глобальный дедуп (caseless, первое вхождение) + **first-fit по фразам** в ≤3 бина по 4096 симв. без пробелов. Детерминированно (порядок наборов и фраз из файла структуры, никаких множеств/сортировок) → повторный прогон даёт ТЕ ЖЕ имена и реюз находит наборы. Имя бина = «A + B», разрезанный исходный набор получает «(часть N/M)», уникальность имён гарантируется. Замер kuderko 4020/1586/2415/4045 = 12 066 симв. при потолке 3×4096 = 12 288 → влезает; не влезло — `res["not_packed"]` + ГРОМКАЯ ошибка со списком, без тихой обрезки. Склейка включается ТОЛЬКО при >3 наборах (при ≤3 имена и поведение прежние).
  - Реюз по имени теперь СВЕРЯЕТ СОСТАВ: `negativekeywordsharedsets.get` читает `NegativeKeywords`, расхождение → ошибка «набор в кабинете отличается от слепка (N фраз в кабинете / M в слепке)». `update` НЕ делаем осознанно: набор — общий объект аккаунта и может висеть на кампаниях директолога, перезапись порезала бы их показы (та же политика, что «матчим только по точному имени»).
  - `apply_minus_sets_batch` отдаёт `error_kind` (`validation`/`transport`) и `minus_set_validation`; оркестратор усекает список до 3 ТОЛЬКО на валидации Директа по наборам, транзиент → повтор ПОЛНЫМ списком (раньше ложный диагноз «лимит Директа» на любом отказе Grid/куки). Непустой `failed_campaigns` теперь тоже идёт в ошибки джобы (ok=True скрывал частичную потерю).
  - `region` (области аккаунта из `ctx["oblasts"]`) наконец доезжает до гео-гарда — «Волгоградская область» больше не остаётся в минусах; `_region_geo_stems` разбирает список через запятую.
- ⚠️ Не переизобретать (раунд 2): (4) отбрасывать 4-й и далее наборы «по лимиту Директа» — запрещено решением Семёна, фразы надо склеивать; (5) чинить расхождение набора и слепка через `negativekeywordsharedsets.update` — это перезапись ОБЩЕГО объекта аккаунта, разрешать только явным решением Семёна; (6) усекать список наборов на ЛЮБОМ неуспехе привязки — транзиент Grid/куки маскируется под лимит.
- **Раунд 3 (2026-07-28, ревью коммита `f33aaef4`, 🟡 ждёт прогона):** имя склеенного набора было конкатенацией имён исходных наборов + «(часть N/M)», то есть ЗАВИСЕЛО ОТ СОСТАВА: любая правка набора в слепке сдвигала границу корзины → менялось имя → реюз по имени промахивался, старый набор навсегда оставался сиротой (лимит 30 на аккаунт, автоуборки нет; упёршись в лимит, получили бы ошибку, которую по прежнему коду никто не видит). Новая схема — `_pack_minus_set_name`: «`{slepok} · {site_type} — минуса N/3`», где N — позиция корзины, а M — КОНСТАНТА `_MINUS_LIB_MAX_SETS_PER_CAMPAIGN` (иначе имя поехало бы при 3 корзинах → 2). Мигрировать нечего: в кабинетах `lib_sets=0`. Заодно снята путаная нумерация «часть N/M» (корзина с хвостом набора называлась «часть 1/2»).
  - Сверка состава при реюзе: один нормализатор `_norm_minus_phrase` на ОБЕ стороны (раньше `_live` нормализовался, а `_want` — нет → фраза с двойным пробелом всегда числилась отсутствующей); «расхождение» = ТОЛЬКО непустой `_missing` (раньше `len(_live) != len(_want)` давало вечное «не хватает 0» на наборе-надмножестве, куда директолог дописал фраз руками).
  - Повтор привязки полным списком — ТОЛЬКО при `error_kind == "transport"` и с паузой `_MS_RETRY_BACKOFF_SEC = 3 c`: на валидации Директа идентичный payload гарантированно падает снова, а мгновенный ретрай попадал в то же окно сбоя куки/Grid.
  - ⚠️ Лимит длины `Name` у `negativekeywordsharedsets` в справке Директа НЕ подтверждён (документирован только лимит 4096 симв. на СОСТАВ), поэтому взят консервативный `_MINUS_SET_NAME_MAX = 100` — новые имена ~35 символов.
- ⚠️ Не переизобретать (раунд 3): (7) включать в ИМЯ набора что-либо от состава (имена исходных наборов, число корзин, счётчик фраз) — это ровно механизм появления сирот; (8) считать расхождением набор-надмножество в кабинете.

### POSEVY_PLAN_IGNORES_SELECTION — план Посевов всегда 12 кампаний, выбор пользователя не читался (2026-07-28)
- Симптом: в дереве набора отмечены только 4 строки tp10 (Telegram+Max), «Итог создания» показывает 4, а `POST /direct/api/set_plan` возвращает `count=12`, `Counter({'tp8':4,'tp9':4,'tp10':4})` → в очередь уходят невыбранные tp8/tp9 (джоба `d5c3f15b1466`, porg-uy3huxcn, остановлена вручную).
- Где: `create_set_plan.py:846` — `for _post_tp in ("tp8","tp9","tp10")` безусловный, `_sel_labels`/`_sel_groups` в ветке посевов не вызывались НИ РАЗУ; список кампаний захардкожен в `_POSEVY_MONO_BRANDS` (:841-845). UI при этом честный: считает по DOM-чекбоксам (`automation_create.js:569-580`), а payload целый (`selectedPositions()`, :482).
- Root-cause: у посевов семантика выбора ОБРАТНАЯ tp1-tp7 — невыбранный tp просто отсутствует в `selected_pos` (в tp1-tp7 его отсекает список `variants`, а посевы включаются флагом `agent_group="posevy"`). Ветка посевов вообще не имела фильтра, а источник (3 tp × 4 бренда) был вторым прочтением структуры — хардкодом рядом с `posevy.json`, который рисует UI.
- Решение (2026-07-28): план строится из ТОГО ЖЕ источника, что питает UI, — `_json("slepki_structure.json")` → `slepki_store.assemble()`, слепок `posevy`, одна кампания на ГРУППУ структуры (`create_set_plan._posevy_positions`, метка = `group.name` = `data-desc` чекбокса, ct — префикс `item.gc`, бренд — хвост имени группы после «—»). Фильтр: есть хоть один ключ `8/9/10` в `selected_pos` → строгий (tp без ключа не создаём); ключей нет вовсе (API/retry/старый клиент) → прежние 12. `_POSEVY_MONO_BRANDS` удалён.
- Детект (офлайн, тот же стенд, что в тестах): `POST /direct/api/set_plan` с `agent_group="posevy"`, `selected_pos={"10":{labels:[4 имени]}}` → было `count=12`, стало `count=4`; одна строка tp10 → было 12, стало 1; пустой `selected_pos` → 12 в обоих.
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_posevy_plan_selection.py` — 10 passed (4/4/4/8/1/12 + прежнее поведение + имена кампаний 1:1 с кодером структуры); `test_create_auto_regressions.py` + `test_metrika_plan_alert.py` — 76 passed.
- ⚠️ Не переизобретать: брать бренд через `create_set_tp8_10._coder_brand_for_ct(ct)` НЕЛЬЗЯ — он ходит в Victory (`campaign_naming._ag_part1_map`), при недоступной БД молча отдаёт `""` → все 4 кампании получат brand_label «Посевы», имена схлопнутся и `_uniq` начнёт минтить `_v01`.
- НЕ помогло ранее: (первый фикс по этой сигнатуре).

### STRUCT_CT0000_NODE_EXPANDS_TO_WHOLE_PACK — узел на ct0000 давал ВЕСЬ пак вместо одной группы (2026-07-28)
- Симптом: `porg-xjxpfxby` (слепок `kuderko`, «С пробегом»), кампании **713097597** и **713097619** («РСЯ - Комби / Комби+Фид - Агрегаторы - Аудитории») — в кабинете **27 групп**, тогда как в структуре у узла «Агрегаторы» ровно **1** группа. Ошибок в отчёте джобы нет.
- Где: tp1/tp5, token-путь, `create_set_tp1_builders._build_tp1_from_pack` (выбор `_units`).
- Root-cause (цепочка, проверена на реальной структуре): `create_set_structure.structure_to_campaigns:450,470` НАМЕРЕННО не кладёт `ct0000` в `cts` (там перечисляются модель-ct — та же конвенция, что у `create_set_text_builders._struct_cts:341-362` «Список модель-ct» и `_struct_items:404`) → у чисто-ct0000-узла `cts=[]` → `create_set_plan:930/948` `tp1_only_cts=[]` → в билдере `_oc=None`; `_struct_items` тоже пропускает ct0000 (`create_set_tp1_builders:2055`) → gk-фильтр даёт `_items=[]`, `_multi=False` → ветка else читала пустой ct-фильтр как «фильтра нет» и брала **весь пак** `sorted(pack.keys())` (27 ct). Существующий ct0000-фолбэк ниже (`if not _units`) не срабатывал, потому что `_units` уже был непустым.
- Решение (2026-07-28): НЕ снимать исключение ct0000 из `cts` (оно защищает `_build_text_from_pack:459-463`, где при splits-формате `cts = list(only_cts)` — ct0000 там породил бы безбрендовую группу с `ct_name/ct_model`-lookup'ом и корневым href), а ввести ЯВНЫЙ маркер узла. `create_set_tp1_builders.py`: новый `_struct_ct0000_units(slepok, site_type, tp_code, only_gks)` (строго ct0000 по `gc` item'а и строго по gk кампании) + в `_build_tp1_from_pack` перед веткой «весь пак» условие `_og is not None and _oc is None and not _items`. Все остальные комбинации (есть модель-ct / нет camp_names-маршрута / есть структурные items) идут прежним путём.
- Детект-запрос (офлайн, по структуре — считает кампании, у которых плановые группы ≠ структурным):
  ```python
  # .venv/bin/python, cwd=home/seoadvanced
  from direct import create_set_tp1_builders as b, create_set_structure as s, slepki_store as ss
  hit = 0
  for dl in ss.assemble()["directologists"]:
      for st in dl["site_types"]:
          for tpc in ("tp1", "tp5"):
              for c in s.structure_to_campaigns(dl["key"], st["name"], tpc):
                  og, oc = set(c.get("gks") or ()), set(c.get("cts") or ())
                  if og and not oc and b._struct_ct0000_units(dl["key"], st["name"], tpc, og):
                      hit += 1
  print(hit)   # 69 из 886 tp1/tp5-кампаний (замер 2026-07-28)
  ```
  Live-детект: Grid `groups_for_edit` по кампании → число групп должно совпасть с `n_groups` узла (713097597/713097619 → 1, было 27).
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_struct_ct0000_node.py` — 6 passed; `test_create_auto_regressions.py` + `test_grid_add_idempotency.py` + `test_architecture_boundaries.py` — 102 passed. По реальной структуре у всех **69** затронутых кампаний `len(units) == n_groups` (1:1 со слепком).
- ⚠️ Не переизобретать: просто убрать `ct != "ct0000"` из `structure_to_campaigns` НЕЛЬЗЯ — `cts` едет как `only_cts` не только в tp1 (`create_set_plan:1056/1122/1218/1289`), и в `_build_text_from_pack` при пустом `_struct_cts` он становится СПИСКОМ ct напрямую.
- НЕ помогло ранее: (первый фикс по этой сигнатуре).

### TP1_TP5_STRUCT_AUDIENCES_NEVER_APPLIED — аудитории структуры tp1/2/4/5 не доезжают никуда (2026-07-28, 🟡 ФИКС ЖДЁТ ПРОГОНА)
- Симптом: `porg-xjxpfxby`, кампании 713097597/713097619 — `retargetings_present = 0/27`, `audienceTargeting = ALL_AUDIENCE 27/27`, хотя в структуре у узла лежат 5 аудиторий (`AUDIENCE:36694733/36694732/36694731/36694734/36694681`).
- Где: `item["audiences"]` для tp1/tp2/tp4/tp5 не читается НИГДЕ; `grid_create_payloads.build_adgroup:134,141-142` жёстко ставит `audienceTargeting="ALL_AUDIENCE"`, `retargetingCondition=None`, `retargetings=[]`.
- Масштаб (замер по `slepki/*.json` 2026-07-28): **1118** items с непустым `audiences` (tp1=1035, tp2=1, tp4=1, tp5=81).
- ⛔ Почему НЕ починено сейчас (а не «забыли»): подход tp6/tp7 к этим данным НЕПРИМЕНИМ. Типы id в tp1/2/4/5: `AUDIENCE`=1603, `RETARGETING`=933, голых=4, `INTERESTS`=**0**; а tp6/tp7 умеет ровно и только `INTERESTS`/`APPLICATION` (`create_set_context._STRUCT_AUD_SUPPORTED:383`, `:415-417` — `AUDIENCE:`/`RETARGETING:` там СЧИТАЮТСЯ ПОТЕРЯМИ и предупреждают) и шлёт их как `ca_retargeting_condition.goals` в **UAC** (`uac_client.py:649-668`) — другая ручка и другая сущность. Для обычных кампаний это групповые `retargetings`/`searchRetargetings` (Grid) либо v5 `audiencetargets.add`, а write-пути для них в репозитории НЕТ ВООБЩЕ: `retargetings: []` захардкожен и на создании (`grid_create_payloads.py:142`), и на апдейте (`grid_finalize.py:3443`), причём `repair_keywords.py:93` и `grid_finalize.py:1730` НАМЕРЕННО пропускают группы с `retargetings_present`, чтобы этот payload их не затёр. Ни HAR, ни примера payload'а с непустым `retargetings`, ни второго значения enum `audienceTargeting` в репозитории нет.
- Что нужно для фикса (внешнее решение, не угадывать): (1) выбрать транспорт — Grid `retargetings`/`searchRetargetings` или v5 `audiencetargets.add`; (2) HAR/пример реального payload'а группы с аудиторией + допустимые значения `audienceTargeting`; (3) подтвердить, что харвестнутые `RetargetingListId` существуют в целевом кабинете (`retargetinglists.get`, ср. `account_service.py:592`); (4) решить поиск (tp2/tp4) vs сеть (tp1/tp5) — это разные поля.
- Фикс (2026-07-28, коммит ниже): механика взята из HAR `direct.yandex.ru.73har.har` + JS-билдера интерфейса (`b10fd987c1079081.chunk.js`), разбор — `.claude/sdd/har-audiences-format.md`. Отдельной ручки нет: аудитории едут тем же full-object апдейтом/созданием группы, поле `retargetings` (сеть tp1/tp5) / `searchRetargetings` (поиск tp2/tp4), элемент = `{retCondId: <id условия>, id: null}`. `audienceTargeting` остаётся `ALL_AUDIENCE`, `retargetingCondition` — `None` (NEW_AUDIENCE требует `currentAudience`; inline-условие — путь INTERESTS/HOST через UAC).
  - Новый модуль `create_set_audiences.py`: чтение `item["audiences"]` (берём только `AUDIENCE:`/`RETARGETING:` — `INTERESTS:`/`HOST:` это goal id, не сюда), индекс `{gk → [(id, имя)]}`, резолв под ЦЕЛЕВОЙ кабинет, сборка payload.
  - Резолв id донора: (1) id есть в целевом кабинете → как есть; (2) иначе матч по ИМЕНИ условия из `item["rl_audiences"][].name` (NBSP-нормализация; 2271 из 2540 ссылок имеют имя); (3) не найдено → НЕ отправляем + видимый warning позиции (`rep["warnings"]`) и строка `[audiences] …` в journal. Карта кабинета — уже читаемый раз-на-джобу `ret_map` (`create_set_orchestrator.py:463` → `remember_account_conditions`).
  - Точки: `grid_create_payloads.build_adgroup` (+`retargeting_ids`/`retargeting_on_search`), `grid_create.create_full:954` (`search_only`), `create_set_tp1_builders._build_tp1_adgroups:349` (tp1/tp5), `create_set_text_builders._build_tp2_adgroups:141` (tp2/tp4), источники групп `_tp1_pack_groups` / `_build_tp1_from_pack` (attach по `gk`), `grid_finalize.build_update_item` (параметризован, дефолт прежний).
- ⚠️ Пропуски `repair_keywords.py:93` и `grid_finalize.py:1730` НЕ снимались — намеренно: `GroupsForEditLite` отдаёт по ретаргетингам только `adGroupId` (без `retargetingConditionId` и без признака поиск/сеть), поэтому RMW-апдейт сохранить уже стоящие аудитории не может; снятие пропуска обнулило бы их.
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_struct_audiences_write_path.py` — 17 passed; `test_create_auto_regressions.py` + `test_grid_add_idempotency.py` + `test_struct_ct0000_node.py` + `test_text_path_signature_contract.py` + `test_architecture_boundaries.py` — 112 passed. Критерий приёмки: `porg-xjxpfxby` 713097597/713097619 — у групп с аудиториями в структуре `retargetings` непуст, у остальных пуст (`grid_finalize.py:3345 GroupsForEditLite`, поле `retargetings_present`; «было» 0/27).
- **Раунд 2 (2026-07-28, ревью коммита `2a087591`, 🟡 ждёт прогона):** первый фикс закрыл только часть путей.
  - **tp2/tp4 на ОСНОВНОМ (token/DIRECT_API_FIRST) пути аудиторий НЕ получали:** `_build_tp2_adgroups` читает `g["audiences"]`, но группы ему строит `create_set_text_builders._build_text_from_pack`, где не было ни `struct_audiences_by_gk`, ни `attach_to_group` → `g["audiences"]` всегда `None`, аудитории доезжали только cookie-фолбэком (`_tp1_pack_groups`). Фикс: тот же `attach_to_group` по `gk` (зеркало `_build_tp1_from_pack:1063/1142`).
  - **tp5 уезжал в СЕТЕВОЕ поле:** `retargeting_on_search=(tp_code in ("tp2","tp4"))` → у tp5 `False` → `retargetings` вместо `searchRetargetings`, при том что репозиторий классифицирует tp5 как Search-канал (`create_set_feed_builders.py:607`, spec `network=False/search=True`). Масштаб — 81 item tp5 с аудиториями. Фикс: `create_set_audiences.is_search_channel(campaign_spec|mode|tp_code)` — канал берётся от КАМПАНИИ (spec `search`/`network`, как `grid_create.create_full:936`, либо `mode` спеки v501), а не от списка tp-кодов; `campaign_mode` прокинут из тех же мест, где кампания создавалась (tp1 → `spec.mode`, tp5/tp2/tp4 → `mode="search"`).
  - **Потеря аудиторий стала видимой:** пустая карта условий кабинета (нет токена / `retargetinglists.get` пуст) при непустых аудиториях структуры → `_add_job_err` в оркестраторе (раньше только warning позиции, который не влияет на `has_issues` и режется до 5 на позицию → джоба зелёная при полной потере). Сбой чтения структуры в `struct_audiences_by_gk` больше не глушится голым `out = {}` — логируется.
- ⚠️ Не переизобретать (раунд 2): признак поиск/сеть по СПИСКУ tp-кодов — это и была причина дефекта tp5; считать только по каналу кампании.
- 📌 **ИЗВЕСТНОЕ ПОВЕДЕНИЕ (не баг, принято раундом 2, зафиксировано 2026-07-28):** аудитории раздаются по бакетам `gk`, а `gk` в структуре НЕ уникален. Замер: 1034 бакета с аудиториями, у **10** из них под одним `gk` лежат РАЗНЫЕ наборы аудиторий (все tp1, слепок `kuderko`) → такие группы получат ОБЪЕДИНЕНИЕ наборов, а не свой. Дизайн предсуществующий (ключ раздачи = `gk`), правкой раунда 2 не вводился. Чинить только по отдельному решению Семёна: ключ раздачи придётся сделать уже (`gk` + имя группы/индекс), это меняет матчинг во всех путях tp1/tp2/tp4/tp5.
- НЕ помогло ранее: —

### KW_LIMIT_COUNTS_MINUS_WORDS — лимит «7 слов» считал минус-слова → ключи терялись целиком (2026-07-28)
- Симптом: `porg-pl6iavd5`, кампании **713096741** и **713096753** (tp1 «Общие - КС») — **все 8 групп в каждой пусты**, при том что в структуре слепка у `ct0010` (Drom, слепок scherbakova) лежит 155 ключей, а по слепку ≈78 тыс. Ошибок в отчёте джобы НЕТ — полное молчание.
- Где: `automation_runtime._kw_clean:2085-2090` — `len(w.split()) > 7` считал ВСЕ токены фразы, включая минус-части.
- Механика: боевая фраза `drom ru продажа авто -запчасти -экзамен -ниссан -договор -крым -уаз -нива -автозапчасти -амур -амурская -гай -спецтехника -улан` = 17 токенов, из них **4 позитивных** и 13 минус-слов → 17 > 7 → фраза выбрасывалась целиком. У таких паков минус-хвост есть почти у каждой фразы → выкашивался весь набор группы.
- **Фактический лимит Директа (проверено по документации, число 7 НЕ менялось):** `.claude/skills/yandex-direct/docs/troubleshooting/interface.md:284` — «Количество слов для одной ключевой фразы — не более 7, без учёта стоп-слов»; `:290`/`:294` — минус-фразы лимитируются ОТДЕЛЬНО (каждая ≤7 слов) и в лимит самой фразы не входят; `:282` — 4096 символов «включая минус-слова» (в коде уже так и было, посимвольную проверку не ослабляли).
- Решение (2026-07-28): в `automation_runtime.py` добавлен `_kw_positive_words(phrase)` (считает слова, не начинающиеся с `-`; аналог `text_gen._kw_positive_tokens`), условие `_kw_clean` переведено на него. **Содержимое фразы не меняется — минус-слова остаются в ключе**, меняется только ПОДСЧЁТ. Реальное превышение (8+ позитивных слов) по-прежнему отсеивается.
- Закрытие молчания: `create_set_tp1_builders.py:374-396` — гейт «0 из N создано» стоял ВНУТРИ `if kw_items:` и при пустом `kw_items` был недостижим. Добавлен внешний гейт: фразы на входе БЫЛИ (`_kw_raw_total > 0`), а `kw_items` пуст → `rep["errors"]` «ключи(tpX): все N фраз отсеяны очисткой на M группах». Строка содержит «ключ» → на tp5 её подхватывает `_synthesize_tp1_build_error` → singular `rep["error"]` (позиция не ok). Чистый автотаргет (`autotarget and not keep_keywords`) отсечён `continue` ДО счётчика — пустой список ключей там норма by design, ошибка не пишется.
- Статус: 🟡 фикс на Mac, ждёт живого прогона (`porg-pl6iavd5`: группы кампаний 713096741/713096753 должны получить ключи, у ct0010 — 155). Офлайн: `direct/tests/test_kw_clean_minus_limit.py` — 10 passed (боевая фраза 4 позитивных + 13 минусов проходит и минусы на месте; 9 позитивных отсеивается; граница 7 проходит / 8 нет и минусы её не спасают; лимит 4096 символов по-прежнему считает минусы; КС-группа с полностью выкошенным набором даёт видимую ошибку; здоровый путь молчит; автотаргет-группа без ключей ошибки не даёт; tp5 → singular error; сквозной путь с настоящим `_kw_clean` довозит фразу до AddKeywords). Полный `direct/tests` (без 3 файлов, требующих БД): БЕЗ моего файла 5 failed / 557 passed, С ним 5 failed / 567 passed — набор падений идентичен, регресса нет (5 падений — чужие правки в работе).
- ⚠️ Не переизобретать: считать длину ключа `len(w.split())` НЕЛЬЗЯ — это ровно та причина. Символьный лимит 4096, наоборот, считается по ВСЕЙ строке вместе с минусами.

### IMAGES_TOPUP_DUPLICATE_CREATIVE — добор до 5 картинок ставил ОДИН креатив дважды (2026-07-28)
- Симптом (скрин Семёна): `porg-pl6iavd5`, кампания **713096702** (`tp1_cpc_site`, «РСЯ - Комби - Марки - КС - Краснодарский край»), группа `ct0300 — Tenet`: в блоке «Изображения» 5 картинок, из них ДВЕ одинаковые — «КАСКО в подарок» на 3-й и 5-й позициях; остальные три разные (Trade-in, Зимние шины, Топливная карта).
- ПРИЧИНА (проверено фактом, не догадка): уникальные НЕ кончились и дубля внутри одного источника НЕТ. `Manual/ct0300/` физически содержит ровно 4 файла; `image_slepki.txt` для `Мультибренд/tp1/ct0300` НЕ содержит строк `scherbakova` → свой слепок даёт 0; 5-м добором каскад берёт ЧУЖОЙ слепок — `_image_store/slepki/karavaev/porg-psm5h7q6/8ZN6fuwhY3sKUKPOJJHFzQ.png`, а это **тот же баннер «КАСКО в подарок / Tenet T7», пересохранённый в другой файл**: путь и имя другие (path-дедуп `dict.fromkeys` слеп), md5 другой (`549664ef…` ≠ `f1937a7e…`, 2021990 ≠ 2126596 байт — байт-дедуп тоже слеп), **pHash совпадает бит-в-бит**. Сверено визуально (оба файла открыты) + `uac_client._image_phash` на LXC101 (Pillow 12.2.0): hamming = **0**.
- Где: `automation_runtime._creative_images_for_ct` — обе auto-ветки каскада дедупили ТОЛЬКО по пути (`dict.fromkeys` / `p not in imgs`).
- Решение (2026-07-28): в `automation_runtime.py` добавлены `_image_identity_key` (pHash → md5 → путь), `_UniqueImagePool` (принимает только визуально уникальные, дубль НЕ занимает слот и НЕ обрывает каскад — за ним берётся следующий уникальный из следующего источника) и `_finish_image_pool` (warn-строки). Каскад источников, его порядок и запрет добора из чужого `ct`/`ct0000` НЕ менялись.
- Порог сравнения — **ТОЧНОЕ равенство pHash (hamming 0), НЕ порог ≥1**: замер по всем **199** папкам `/opt/neuro_content_local/_manual/ct*` дал минимальную дистанцию между РАЗНЫМИ легитимными креативами одного шаблона = **6** (гистограмма минимумов: 6→3 папки, 8→18, 10→45, 12→69, 14→51, 16→13). Любой порог ≥6 схлопнул бы «Зимние шины» и «Топливную карту» в одну картинку.
- ⚠️ Побочная находка (НЕ чинил, вне задачи): `uac_client.collect_image_files` (tp6/tp7) дедупит визуально с порогом **10** — по тому же замеру 66 из 199 ct имеют легитимную пару на дистанции ≤10, т.е. UAC-пул там может недосчитывать 1 картинку. Проверять отдельно.
- Дефицит уникальных не теряется молча: `[images-pool-short] IMAGES_POOL_SHORT …` + `[images-dedup] …` в журнал воркера (по образцу `UAC_IMAGES_POOL_SHORT`, дедуп строк по (slepok,tp,ct,n,dropped)); для tp6/tp7 короткий пул и так уезжает warning'ом позиции через `create_set_master_product`. **Создание НЕ блокируется** — правило Семёна «лучше 4 разных, чем 5 с повтором» и «5 — потолок Яндекса, а не минимум» (`maxNumberOfImages: 5`, `minNumberOfImages` не существует).
- НЕ помогало ранее: дедуп по ПУТИ (`dict.fromkeys`, был до правки) и дедуп по **md5** — пересохранение креатива меняет и то и другое. Для не-авто (`dmp`) pHash-уровень остаётся ВЫКЛЮЧЕННЫМ осознанно (запись про dmp-баннеры одного шаблона с разным текстом) — ветка `_slepok_is_auto=False` не тронута.
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_image_pool_unique_topup.py` — 7 passed; полный `direct/tests` (без 3 файлов, требующих БД) — БЕЗ моего файла 5 failed/550 passed, С ним 5 failed/557 passed (набор падений идентичен, регресса нет; 5 падений — чужие правки в работе).

### GRID_CREATE_RETRY_DUPLICATES_ADGROUPS — ретрай Grid-мутации создавал дубль-блок групп (2026-07-28)
- Подтип `RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD` (см. запись 2026-07-18): та сигнатура была закрыта в `yandex_gateway` (v5) и в `grid_finalize` (`_post` без ретрая + отдельный `post_idempotent`), но в **`grid_create.py`** — нет.
- Симптом (боевой факт): `porg-nqavjicg`, кампания `713102313` — **14 пустых групп-сирот** (0 объявлений, 0 реальных ключей) рядом с 14 полноценными того же имени. Id-блоки `5777472935..948` (пустые) и `5777472963..976` (полные); у соседних кампаний ровно ОДИН блок из 14.
- Механика: первый `AddUnifiedAdGroups` **реально закоммитил** 14 групп, но ответ вернулся с транзиентом → `_mutate` повторил мутацию → создался второй блок. `ag_ids` пришли от ВТОРОГО вызова, поэтому ключи и объявления ушли туда, а первый блок остался мусором. В `STAGE_TIMING` этого item'а ровно ОДИН `grid:AddUnifiedAdGroups`, `ms=6022` — самый долгий из 160 при типовых 1.8–3.5 с (значит это не ретрай уровня `CAMPAIGN_NOT_FOUND`, у которого было бы два STAGE_TIMING).
- Где: `grid_create.py:_mutate` (транзиент + backoff 0.6 с, применялся ко ВСЕМ операциям, включая `Add*`).
- Решение (2026-07-28): (1) `_creates_objects(op)` — слепой повтор запрещён для любой `Add*`-мутации (`AddCampaigns`/`AddUnifiedAdGroups`/`AddAdaptiveTextAds`/`AddShoppingAds`); исключение только `AddKeywords` (Директ схлопывает одинаковые фразы и на повтор возвращает keywordId существующей — живой зонд, см. `unique_keyword_ids`). Идемпотентные операции (`AdGroupNames`, `Delete*`) ретраятся как раньше. (2) `GridCreateError.transient` + `_response_lost()`: «ответ потерян» = транзиент Директа, не-JSON ответ, обрыв соединения. (3) Устойчивость к РЕАЛЬНОЙ потере даёт не ретрай, а `_add_adgroups_reconcile`: перечитываем факт (`_read_adgroup_name_to_id_strict`, 2 попытки с паузой 2 с под лаг реплики) и сравниваем с отправленными именами — все на месте → НЕ создаём ничего; ни одной → безопасный повтор; часть → досоздаём только отсутствующие. Состояние не прочиталось / несколько кампаний в чанке / неуникальные имена → исходная ошибка наружу (вслепую не пересоздаём). (4) Гейт **«создано групп ≠ отправлено»** (`_gate_groups_created`) в 4 точках `grid_create.py` — раньше расхождение проходило молча.
- **Доработка по ревью (2026-07-28, вердикт ❌ → правки):** первая версия чинила дубли, но ломала две вещи.
  * **Гейт стал приговором.** `_gate_groups_created` писал расхождение в `rep["errors"]`, а в куки-пути tp2/tp4 (`create_set_feed_builders._create_text_via_cookie:196-218`) ЛЮБОЙ непустой `errors` = `_delete_partial_campaign` + `defer`. «Создано 13 из 14» сносило кампанию с 13 РАБОЧИМИ группами. Теперь расхождение едет в `rep["groups_expected"]` / `rep["groups_shortfall"]` / `rep["warnings"]`, а видимость даёт верификатор: новый код **`GROUPS_CREATED_LESS_THAN_SENT`** в `local_result_verifier` (severity=error, **report-only**, как `RSYA_NOT_FINALIZED`) → попадает в `verification.summary.errors` → `has_issues`, но кампанию не трогает. `build.groups_expected` прокинут в `create_set_feed_builders` (2 точки) и `create_set_tp1_builders` (`tp1_build`).
  * **Ретрай сняли, а сверку сделали только для групп.** Транзиент на `AddCampaigns` давал отказ позиции + кампанию-сироту (id неизвестен, cleanup не зовётся), на `AddAdaptiveTextAds` — «0 ads», а это в tp1 ведёт к `delete_campaigns`. Добавлены сверки тем же приёмом: `_add_campaign_reconcile` (read-back кампаний по имени, `CampaignNames`; принимаем найденную ТОЛЬКО если она ровно одна И у неё ещё нет групп — иначе это чужая одноимённая, ошибка наружу) и `_ads_reconcile` для `AddAdaptiveTextAds`/`AddShoppingAds` через `_read_ads_agid_map_strict(cid)` (`add_ads`/`add_shopping_ads` получили kwarg `campaign_id`; без него — прежнее поведение, ошибка наружу).
  * **Классификация — allow-list, а не префикс `Add`.** `_creates_objects` теперь считает создающей ЛЮБУЮ мутацию вне `_IDEMPOTENT_OPS` (`AdGroupNames`/`AdsAgid`/`CampaignNames`/`Callouts`/`AddKeywords`) и `Delete*` — как `yandex_gateway._creates_objects` («дубль дороже отказа»). Иначе `CopyCampaigns`/`CreateSitelinkSet` молча получили бы слепой ретрай.
  * **Коллизии имён групп.** Сверка по имени сравнивала имена ЧАНКА (50) с именами ВСЕЙ кампании. В слепках есть коллизии `gk` → одноимённые группы в одной кампании (194 на `porg-nqavjicg`) → группа считалась бы созданной, а её ключи/объявления уехали бы на чужой `adGroupId`. Теперь: снимок имён кампании снимается ДО мутации (`_adgroups_reconcile_ctx`; `create_full`/`create_shopping_full` передают `campaign_is_new=True` и снимок не читают — лишнего запроса в горячем пути нет) + уникальность имени проверяется по ВСЕМУ вызову, а не по чанку. Неоднозначно (дубль имени / имя было занято до мутации / снимок не снят) → исходная ошибка наружу.
- **Доработка №2 по ревью (2026-07-28, вердикт ❌ → правки):** гейт был виден лишь в ОДНОМ пути из четырёх, а «не знаю» трактовалось как «не найдено».
  * **Гейт доезжает во ВСЕХ путях.** `rep["groups_expected"]`/`["warnings"]` из `_gate_groups_created` выбрасывали консьюмеры: build товарки tp3/tp5 (`create_set_feed_builders._create_shopping_via_cookie`) собирал только `groups/ads/shopping_ads/feed_id/errors`, а строка репейра (`repair_content.execute_content_repair`) — только счётчики. «Создано 13 из 14» проходило молча и там, и там. Теперь: товарный build несёт `groups_expected`/`warnings`; репейр получил `_attach_group_shortfall` — поля в строку + прогон ТОГО ЖЕ `verify_local_result` (issues в `row["issues"]` и в ответ `verification_issues`). `ok` строки НЕ трогаем — report-only, иначе повторяем ошибку «гейт в errors» (см. «НЕ помогло ранее»).
  * **Обрыв скана кампаний ≠ «кампании нет».** `_read_campaign_ids_by_name_strict` листал `while offset <= _CAMPAIGN_SCAN_MAX` и на аккаунте >4000 кампаний молча возвращал `[]` при `read_ok=True` → `_add_campaign_reconcile` считал «коммита не было» → `_add_campaign_once` → **ДУБЛЬ кампании**. Теперь при обрыве — `GridCreateError` («состояние аккаунта неизвестно»); reconcile ловит её как «сверка не удалась» и пробрасывает исходный транзиент, вслепую не создавая.
  * **Архивная одноимённая не «усыновляется».** read-back запрашивает `status{primaryStatus archived}` (форма из живого `yandex_gateway.grid_list_campaigns:321`) и отбрасывает архивные: пустая архивная кампания прошлого прогона проходила обе проверки (ровно одна + без групп), и группы набора уехали бы в архив.
  * **Лишнее чтение в горячем пути.** `campaign_is_new` проброшен параметром (дефолт False) до Фазы 1 главных путей: `create_set_tp1_builders._build_tp1_from_pack→_build_tp1_adgroups` (оба каллера создают кампанию шагом выше) и `create_set_text_builders._build_text_from_pack→_build_tp2_adgroups` (shell TEXT_CAMPAIGN создан шагом выше). Фазе 4a («все фиды», tp1_builders:759/776/2587) флаг НЕ передан ОСОЗНАННО: там кампания уже содержит группы Фазы 1, и предмутационный снимок — единственная защита от совпадения имени «Товарная галерея · <фид>».
  * Из `_IDEMPOTENT_OPS` убран `Callouts` — такой операции в `grid_create` нет (скопирована из `yandex_gateway`).
- НЕ трогали: ретрай `CAMPAIGN_NOT_FOUND` (eventual consistency после `campaigns.add`, идемпотентен по смыслу), реавторизацию по 403 — протухшая кука по-прежнему переподхватывается, разведение «read вернул `{}`» vs «read упал» (`*_strict`-читатели), исключение `AddKeywords`, асимметрию групп/объявлений (долг), `uac_client.collect_image_files` порог 10 (отдельная задача), `grid_finalize.GridClient.add_shopping_ads` (непокрытый пробел).
- Статус: 🟡 фикс на Mac, ждёт живого прогона. Офлайн после доработки: `direct/tests/test_grid_add_idempotency.py` — **22 passed** (+ консьюмер-кейс «13 из 14 → кампания НЕ удаляется, `GROUPS_CREATED_LESS_THAN_SENT` виден», read-back AddCampaigns ×3, сверка объявлений ×4, allow-list классификации, кросс-чанковый дубль имени, имя занятое до мутации). Первая версия: 9 passed (коммит-до-транзиента без пересоздания; потеря до коммита → создание всё равно происходит; частичный коммит → досоздаются только missing; нечитаемое состояние → ошибка без пересоздания; валидация не сверяется; `Add*` не ретраится, `AdGroupNames`/`AddKeywords` ретраятся; `CAMPAIGN_NOT_FOUND` жив; гейт числа групп). Регресс-набор `test_create_auto_regressions`+`test_create_review_findings`+`test_architecture_boundaries`+`test_job_status_gate` — 107 passed.
- Статус доработки №2: 🟡 на Mac, ждёт живого прогона. Офлайн: `direct/tests/test_grid_add_idempotency.py` — **32 passed** (+3 пути видимости гейта, обрыв скана → исключение и отсутствие пересоздания, архивная одноимённая, `campaign_is_new` без лишнего чтения); `test_create_auto_regressions`+`test_create_review_findings`+`test_job_status_gate`+`test_architecture_boundaries` — 118 passed, 1 failed (`test_worker_and_copy_import_without_loading_blueprint` — локальный Python 3.9 не понимает `X | None` в `automation_runtime.py:266`, дефект СРЕДЫ, файл не трогался).
- НЕ помогло ранее: **писать расхождение «создано ≠ отправлено» в `rep["errors"]`** (первая версия фикса) — в куки-пути tp2/tp4 это равносильно удалению кампании с рабочими группами; расхождение обязано быть видимым, но не разрушительным. ⚠️ Не переизобретать: ретрай `Add*` внутри `_mutate`/`_post` возвращать НЕЛЬЗЯ ни под каким backoff — это ровно то, что дало 14 сирот; и снимать запрет ретрая без сверки факта тоже нельзя — это меняет дубль на потерю позиции (~1 транзиент на 160 мутаций).

### CREATE_SET_KEYWORD_PACK_GAP_409 — ВЕСЬ набор отбит на входе из-за 0 ключей у ОДНОГО ct (2026-07-28)
- Симптом: `POST /direct/api/create_set_async` → **HTTP 409** ещё до создания любого объекта: «content-gap preflight: local/M3 source доступен, но нет keywords для `<slepok>/<site_type>`: `tp2/ct0283, tp4/ct0283`. Создание не запущено; нужен top-up keyword pack или исключение позиции из структуры (checked=52)». Боевой прогон 2026-07-28: отбиты 3 из 7 аккаунтов — `porg-nqavjicg` и `porg-dmwfp3dk` (terehov/«С пробегом», ct0283) и `porg-uy3huxcn` (salamahin/Мультибренд, ct0885+ct0051). Потеряно 277 плановых позиций из 405.
- Где: `create_set_content_preflight.py:22-73` (`create_set_pack_gap_note`), гейт зовётся до постановки джобы.
- Механика: для КС-позиций tp2/tp4 (`_requires_keyword_pack`: в имени «КС»/«ключев» И не чистый автотаргет) читается `kontent_pack.read_keywords(site_type, tp, ct, slepok, group)`; `positive == 0` хотя бы у ОДНОЙ пары (ct, gk) → блокируется **весь набор**, а не позиция. У `uy3huxcn` дырявыми были 2 ct из 67 в мульти-ct позиции — остальные 65 групп создались бы нормально.
- Проверка, что это НЕ путь/не код: прямой вызов `kontent_pack.read_keywords` на LXC101 с `NEURO_PACK_MOUNT=/opt/neuro_content_local` по всем 6 парам → `positive=0, negative=0`, при этом остальные 50/204 проверенных пар ключи имеют. Точечный дефицит контента.
- Решение: (а) top-up keyword pack для ct0283 (terehov/«С пробегом») и ct0885+ct0051 (salamahin/Мультибренд) — зона `direct_content_harvester`/`direct_slepki_master`; либо (б) убрать эти ct/позиции из структуры слепка.
- Статус: 🟡 открыто, решение за Семёном. Кодом НЕ обходил: гейт намеренный, тихо ронять позиции нельзя.

### CONTENT_GAP_IMAGES_LOW_BLOCKED_CAMPAIGN — позиция не создавалась из-за 4 картинок вместо 5 (2026-07-27)
- Симптом: прогон `3f56db987ab9` отбил `tp7_cpc_site_ct0111_aon … ТК - Haval - КС + Автотаргетинг`: «CONTENT_GAP_IMAGES_LOW: пул картинок для ct=ct0111 slepok=scherbakova — 4 шт. при необходимых ≥5. Кампания не создана». То же на `69a140093e78`. Кампании нет вообще, хотя 4 картинки есть.
- Root-cause (ФАКТ, не гипотеза): порог 5 был скопирован из **ПОТОЛКА** Яндекса и прочитан как минимум. Собственные validation-константы Директа `constants.validation.adConstants` (`GET https://direct.yandex.ru/wizard/web-api/aggregate`, HAR `direct.yandex.ru.5har.har`) содержат `"maxNumberOfImages": 5` и **не содержат** `minNumberOfImages`. Живое создание с меньшим числом креативов API принимает: `POST /web-api/uac/campaigns?ulogin=porg-h27zek57` → **HTTP 201** при `content_ids` длиной **1** (HAR 4har, кампания 710818592); `…?ulogin=porg-riga5gvo` → **HTTP 201** при длине **3** вместе с `feed_id`+`listings_feed_id`, т.е. на ТОВАРНОЙ tp7 (HAR 5har, кампания 710844579). Наш `uac_client.py:87,679-699` и сам трактует 5 как «грузим ДО N».
- Решение (2026-07-27, решение Семёна «мне нужна кампания даже с 4 изображениями»): (1) `create_set_master_product.py` — preflight-блокировка по `_pool_size < 5` удалена, остался только вырожденный блок `CONTENT_GAP_NO_CREATIVE` (0 картинок И 0 видео, порог env `DIRECT_UAC_IMAGES_CREATE_MIN`, дефолт 1, `0` снимает совсем); в результат позиции уезжает `images_pool` и warning `IMAGES_POOL_SHORT`. (2) `uac_verifier.py` развёл два случая: пул < цели и взяли всё → **warn `UAC_IMAGES_POOL_SHORT`** (кода НЕТ ни в `repair_planner._RECREATE_CODES`, ни в `repair_gate._UAC_REPLACE_CODES` → recreate-действие не планируется вовсе); пул ≥ цели, а в кампании меньше → **error `UAC_IMAGES_LOW`** + repair. (3) `live_verifier.py` прокидывает `images_pool` (0 новых запросов). Порог — env `DIRECT_UAC_IMAGES_MIN` (дефолт 5).
- Антицикл (почему `создать-удалить-пересоздать` не возвращается): при коротком пуле recreate-код не эмитится вовсе; при полном пуле попытка ровно ОДНА — дочерняя repair-джоба несёт `_repair_parent_job_id`, а `repair_auto.auto_recreate_request:452-457` по такому телу возвращает `None`. Плюс прежний пересчёт пула в `create_set_repairing._queue_recreate_repair_job` остаётся страховкой для джоб без `images_pool`. Тест `tests/test_uac_images_soft_threshold.py::test_recreate_happens_once_and_never_twice`.
- Верификация: `py_compile` 3 файлов OK; `direct/tests/test_uac_images_soft_threshold.py` — 8 passed; вместе с `test_create_auto_regressions/test_three_new_detectors` — 119 passed (2 падения окружения, не по правке: `test_architecture_boundaries` под python3.9 CommandLineTools и `No module named 'home'`).
- Статус: ✅ ПОДТВЕРЖДЕНО живым прогоном `96f76846fc68` (porg-pl6iavd5, scherbakova/Мультибренд, `single_feed=true`, 26 items, 2026-07-28 00:01–00:44 +05): `ТК - Haval - КС + Автотаргетинг` (ct0111, пул 4) **СОЗДАНА, id 713096179, DRAFT**, `ok=true`, `blocked=null`, `issue_code=null`, `images=4`, `sitelinks=8`. Live-верификация: единственный issue — **`UAC_IMAGES_POOL_SHORT`, severity=warn** (`pool=4`, `expected=5`, note «пересоздание не планируется»); `UAC_IMAGES_LOW` НЕТ, `CONTENT_GAP_IMAGES_LOW` НЕТ. `repair_gate.recreate_delete_campaigns=0`, `queued_recreate_items=0`, `actions=0`; в journal воркера 0 строк по `recreate|пересозда|удаля.*кампан|delete_campaign`. Джоба `done`, created=26/failed=0 (база `3f56db987ab9`: error, 25/1).
- НЕ помогало ранее: сама блокирующая preflight-проверка (введена утром 2026-07-27 против живого цикла delete→recreate на `porg-pl6iavd5`) — цикл она остановила, но ценой несозданной кампании; заменена разведением warn/error по фактическому размеру пула. ⛔ Добор картинок из чужого `ct`/`ct0000` не рассматривался и остаётся запрещённым (`DOD.md` §1.b — строки `UAC_IMAGES_LOW`/`UAC_IMAGES_POOL_SHORT`/`CONTENT_GAP_NO_CREATIVE`; §2.0 «Картинки»).

### AD_HREF_ROOT_INSTEAD_OF_MODEL — квиз-оффер в фиде уводит href объявления на корень домена (2026-07-27)
- Симптом: tp2/tp4 объявления марок Kaiyi/Knewstar ведут на `https://newautos-193.site` вместо страницы марки. Живой замер `porg-pl6iavd5`: `TOTAL_TEXT_ADS 207 · ROOT_HREF 6 · BRANDLESS_/auto 14`.
- Root-cause (доказан диагностикой, гипотеза «пустой brand» ОПРОВЕРГНУТА: `_valid_pack_brand_name("ct0154","KAIYI")='KAIYI'`): `_pack_group_href` берёт URL из фида ПЕРЕД формулой, а единственный оффер этих марок в account-мёрже приходит из **квиз-фида** — `_account_offer_urls['kaiyi'] = 'https://newautos-193.site/quiz?fid=…#x7-kunlun-i-suv-5d'`. Дальше `link_check.strip_quiz_url` (`link_check.py:44-55`) схлопывает любой `/quiz` в голый корень домена. Таких «квиз-ключей» в карте офферов 14.
- Где: `create_set_text_builders.py:45-48` (tp2/tp4) · `create_set_tp1_builders.py:45-48` (tp1/tp5 без `_multi`) · `create_set_master_product.py:303-313` (tp6/tp7) · `create_set_tp8_10.py:219-223` (посевы) — одна и та же фид-first схема без квиз-гарда.
- Решение (2026-07-27): общий хелпер `model_urls._is_degenerate_feed_url` (пусто / голый корень / путь `/quiz`) — ОДИН на все 4 пути, ставится ПОСЛЕ фид-лукапа и ДО `link_check`. Вырожденный фид игнорируется, href строится формулой `_model_page_href` по `real_brand`. `strip_quiz_url` не трогали. Легальный `/auto` (UAZ нет в фидах → `/auto/uaz` → 404 → `_parent_path`) под гард не попадает: он рождается ПОСЛЕ гарда, в resolve-цепочке, а гард смотрит только на URL из фида.
- Верификация: `py_compile` 5 файлов OK; 219 passed по всем тестам, трогающим изменённые модули (8 новых: таблица гарда, квиз-оффер tp2, каталог-оффер без изменений, не-брендовое имя группы, tp1 без `_multi`, гард-контракт tp6/tp7, посевы, UAZ→`/auto`).
- Доработка по ревью (2026-07-27, вердикт ❌): (1) ветка `_formula_name = real_brand or uname` в `_pack_group_href` (tp2/tp4) **удалена вместе с параметром** — для самого дефекта она не нужна (у Kaiyi/Knewstar `real_brand` непуст), а срабатывала ровно там, где `_valid_pack_brand_name` только что отверг имя как не-марку → формула по теме (`Автокредит…`, `Abto`→`Авто`→`/auto/avto`) воскрешала `BUTTON_404_GENERIC_AVTO` на ≥322 структурных item'ах. Для не-марок вернулось прежнее поведение — корень сайта. (2) `_is_degenerate_feed_url`: регексп получил `re.I` — `HTTPS://site/quiz` гард раньше пропускал, а `strip_quiz_url` (urlsplit нормализует схему) всё равно схлопывал такой URL в корень.
- Статус: ⚠️ ЧАСТИЧНО подтверждено прогоном `96f76846fc68` (2026-07-28, полный набор 26 items): **`TOTAL_TEXT_ADS 1325 · ROOT_HREF 0 · BRANDLESS_/auto 47`**. Корневых ссылок нет — основной дефект закрыт. Но ожидание «`/auto` вернётся к ~14 / останется 2 (UAZ)» **НЕ выполнено**: 47 воспроизвелось байт-в-байт с базой `3f56db987ab9` (тот прогон уже шёл на коде без `or uname`, поэтому `99c21fc8` числа и не менял). Разбивка 47 (детектор `/tmp/_href2.py`): **16** — 2 кампании «Общие» × 8 не-марочных тем (Buynew/Tradein/Avtoru/Drom/Oficdealernew/Avtosalon/Avito/Avtocredit), это осознанный корень раздела, а не дефект; **28** — Dongfeng (ct0066/0067/0070/0072), марки нет на сайте → `/auto/dongfeng` 404 → `_parent_path`; **3** — UAZ (ct0256 ×2, ct0258). Число «14» приходило из КОРОТКОГО прогона (207 объявлений / 9 кампаний) и с полным набором (1325/24) несопоставимо: доля `/auto` упала 6.8 % → 3.5 %. Детекторы: `lxc101:/tmp/_detect_root_href.py <логин>`, `lxc101:/tmp/_href2.py <логин>`.
- ⛔ **ЛОЖНАЯ ТРЕВОГА, не заводить снова (2026-07-28):** «сегмент **Общее** ведёт на корень домена» — это **ШТАТНОЕ** поведение, а не остаток дефекта. Не-брендовые `ct` («Автокредит», «Автосалон», «Рассрочка», «Авито», «Дром») не имеют марки → `_valid_pack_brand_name` возвращает `''` → `_pack_group_href` (`create_set_text_builders.py:56-58`) отдаёт корень сайта. Формульный deep-link по ТЕМЕ звать нельзя — именно он воскрешает `BUTTON_404_GENERIC_AVTO` (`/auto/avto`) на сотнях item'ов (см. «Доработка по ревью» выше). Проверено `curl` на `bucars-kuban.site`: корень `200`, `/catalog/avtokredit` и `/rassrochka` — **404**, страниц под темы на сайте НЕТ. Решение Семёна: оставить как есть, на раздел не переводить. **Критерий дефекта — ТОЛЬКО «марка/модель → корень»**; суммарный `ROOT_HREF` без разбивки по сегменту диагностически бесполезен (делить умеет `lxc101:/tmp/_detect_root_href_v2.py`). Зафиксировано в `DOD.md` §1.13.
- НЕ помогало ранее: (1) `TP5_MODEL_HREF_ROOT` — обход `_multi and _uname` в tp1 закрыл только multi-группы tp1, механизм остался сломан (⚠️ ЧАСТИЧНО, 18/1325 корневых); (2) гипотеза «пустой brand у tp2» — опровергнута живым вызовом `_valid_pack_brand_name`; (3) `link_check` fail-open по таймауту невиновен — он сохраняет ИСХОДНЫЙ url.

### COPY_V5_LOSES_RCODE_AND_FEED_FILTERS — копирование: группы с r источника, товарные/каталожные без фильтров (2026-07-27)
- Симптом: копия `porg-mjyh6hjv → porg-ln7tz7xh` (job `274b84c27dca`, Уфа): 102 группы приехали с `_r0088_` (Краснодарский край) вместо `_r0066_` (Республика Башкортостан); 204 товарных/каталожных объявления созданы **без единого фильтра** (`feedFilter=null`, `FeedFilterConditions=null`), хотя в источнике фильтр есть у всех 204 (`url CONTAINS_ANY ["Tenet"]` в «Марках», `collectionId EQUALS_ANY ["model_44"]` в «Моделях»).
- Где: `direct_copy.py:1453` (`"Name": g["Name"]` — имя группы льётся как есть) и `direct_copy.py:1645/1671` (FeedFilterConditions в теле `ads.add`).
- Механика: копия пошла **v5-веткой** (`phase_upload`), а не Grid/ЕПК-веткой. (а) Гео-переписывание снимка меняет только СЛОВОФОРМЫ; регион в кодере зашит КОДОМ `ag_part4` — словами не задеть. Ремап r был только в `copy_grid_unified.py:326`. Резолв региона исправен (`_copy_target_region_code('Уфа',…) → 'r0066'`) — его просто никто не звал. (б) v501 `ads.add` **принимает** `FeedFilterConditions` и не отдаёт ошибку, но на ЕПК ShoppingAd/ListingAd не применяет: объявления создаются с пустым фильтром. Единственный подтверждённый писатель — Grid `updateShoppingAds`/`updateListingAds` (`grid_finalize.set_product_feed_filters`), как и в пути создания РК.
- Усугубляло: `copy_verify` баг ВИДЕЛ (`shopping_filter_signature`/`listing_filter_signature` = mismatch по обеим кампаниям), но writer'а под эти размерности в `run_copy_repair` не было → `repair_gate` пустой, джоба закрылась как `done`. `shopping_filter_count` при этом `ok` — он считает ЧИСЛО объявлений, а не фильтры.
- Решение (2026-07-27, коммит `3f0a5fa6`): `copy_geo._copy_remap_snapshot_region_code` правит имена групп/кампаний в снимке ДО `phase_upload` (вызов в `copy_engine`, `mode != 'other'`); `copy_postprocess._copy_apply_product_filters` переносит feedFilter источника Grid-мутацией после создания (покрывает и v5-, и cookie-созданные); `copy_verify_repair._repair_product_filter_signatures` чинит обе signature-размерности в авторемонте; незакрытые после ремонта repairable-строки → `rep["verify_unresolved"]` + `⚠️` в лог джобы.
- Статус: ✅ проверено живым кабинетом — 102/102 группы → `r0066`, 204/204 фильтра совпали с источником пофрагментно (сверка по `id_maps.json`), `DefaultTexts` не затёрты, повторный прогон ремонта идемпотентен.
- НЕ помогало ранее: — (первое наблюдение этой сигнатуры).

### JOB_STATUS_ERROR_SKIPS_HAS_ISSUES — при `failed>0` разбивка `has_issues` не пишется вовсе (2026-07-27)
- Симптом: контрольный прогон `69a140093e78` (26 items, created=25, failed=1) закончился со статусом **`error`**, а не `done`. В `result` НЕТ ключа `has_issues`, хотя live-верификатор нашёл 23 ошибки — карточка/потребитель разбивки `lv_errors`/`ver_errors` её не получает.
- Где: `queue_server.py:2179-2188` — `compute_job_issues_breakdown(...)` вызывается ТОЛЬКО внутри `if _st == "done":`. Статус считает `create_job_status.terminal_status_for_job` (`failed>0` → `error`).
- Механика: гейт `has_issues` задумывался как «кампании созданы, но с дефектами» поверх `done`. Когда хотя бы один item упал (в т.ч. штатной блокировкой контента `CONTENT_GAP_IMAGES_LOW`), статус уходит в `error` и разбивка не считается вовсе. Числа при этом ЕСТЬ, но только в исходных секциях: `live_verification.summary.errors=23`, `verification.summary.errors=1`, `verification.summary.warnings=22`.
- Решение (2026-07-27): гейт вынесен из ветки `done`. Новая `create_job_status.annotate_job_issues(kind, data)` зовётся в `queue_server.py:2179-2190` на ВСЕХ терминальных статусах этого пути (`done`/`error`/`cancelled`); статус от разбивки по-прежнему не зависит, поведение чистого `done` не изменилось. Верификации не было (нет `summary` ни в `verification`, ни в `live_verification` — например деградированный/timeboxed постпроцесс) → вместо лживых нулей пишется `result["has_issues_unknown"]=true` (`has_verification_data`). `interrupted` не покрывается сознательно: он ставится SQL-апдейтом recover/watchdog (`queue_server.py:244,2402`, `job_repository.py:348`) мимо `result`, верификация там не отрабатывала. UI: `automation_jobs.js` показывает разбивку и в карточке `error`, плюс явную строку «верификация не выполнялась».
- Статус: ✅ ПОДТВЕРЖДЕНО живым прогоном `3f56db987ab9` (porg-pl6iavd5, 26 items, created=25/failed=1 → `status=error`): в `result` есть `has_issues` с `lv_errors=0`, `ver_errors=1` — совпадает с `live_verification.summary.errors` / `verification.summary.errors`. Разбивка больше не молчит на `error`.
- НЕ помогало ранее: — (первое наблюдение этой сигнатуры).

### TP24_TOKEN_PATH_KEEP_KEYWORDS_NOT_IN_SIGNATURE — tp2/tp4 token-путь падал TypeError за ~0 сек (2026-07-27)
- Симптом: на живой джобе `4bce0676297a` 4 item'а tp2/tp4 упали мгновенно (вклад в `failed=5`): `TypeError: _create_text_via_token() got an unexpected keyword argument 'keep_keywords'`. Кампания-оболочка даже не создавалась — падение до первого сетевого вызова.
- Где: вызов `create_set_text.py:83` (`create_text_via_token(**cookie_kwargs)`, набор собран на `:53-79`, ключ `keep_keywords` на `:61`); приёмник `create_set_feed_builders.py:374 _create_text_via_token`.
- Root-cause: коммит `f970f097` добавил режимный флаг `keep_keywords` вызывающему и **в тело** token-функции (`create_set_feed_builders.py:429` прокидывает его в `_build_text_from_pack`), но забыл добавить в её **сигнатуру**. Cookie-близнец `_create_text_via_cookie:94` параметр имеет, и `run_create_set_text` шлёт в оба пути ОДИН и тот же kwargs-набор («token_kwargs == cookie_kwargs»). Тот же класс дефекта, что `TP7_BUILD_NAME_WRAPPER_DROPPED_TARGETING_LABEL` (2026-07-20).
- Решение (2026-07-27): `keep_keywords: bool = False` добавлен в сигнатуру `_create_text_via_token` на ту же позицию, что у cookie-близнеца (после `autotarget`, до `segment`) — семантика на tp2/tp4 применима и уже реализована ниже по цепочке: `_build_text_from_pack` → `_build_tp2_adgroups` (`create_set_text_builders.py:74` `wants_keywords = (not autotarget) or keep_keywords`, `:168` `if autotarget and not keep_keywords: continue`), т.е. группа чистого автотаргета ключей не несёт, а `КС + Автотаргетинг` — несёт. Новый тест `tests/test_text_path_signature_contract.py` берёт РЕАЛЬНЫЙ набор ключей вызывающего из AST и биндит его настоящими сигнатурами обоих путей (`inspect.signature().bind`), плюс запрещает «починку» через `**kwargs`.
- Верификация: локально `py_compile` OK, `4 passed` новый файл, `65 passed` вместе с `test_create_auto_regressions/test_create_review_findings/test_architecture_boundaries`; на LXC101 `/root/venv` `4 passed`, md5 Mac==LXC101. Негативный контроль: та же bind-проверка против HEAD-сигнатуры даёт ровно `TypeError ... 'keep_keywords'`. AST-скан всего пакета на вызовы с несуществующими именованными параметрами (включая `f(**kwargs_dict)` и DI-шимы `*args/**kwargs`) после фикса — 0 находок.
- Статус: ✅ ПОДТВЕРЖДЕНО живым прогоном `69a140093e78` (porg-pl6iavd5, 2026-07-27 18:15→19:00 +05, `DIRECT_API_FIRST=1`): 4 item'а tp2/tp4 отработали 107.0 / 57.9 / 126.1 / 53.3 сек (не ~0), в логе прогона 0 вхождений `TypeError ... keep_keywords`; кампании созданы и наполнены (tp2 88 групп/8657 реальных ключей и 36/4351; tp4 150/14489 и 36/4330).
- НЕ помогло бы: глушить параметр через `**kwargs` в приёмнике — спрятало бы и этот дефект, и следующие такие же (тест это теперь запрещает).

### METRIKA_GOAL_MISSING_VIA_API_PATH — «укажите цель (goal_id)» при ПРОГРАММНОМ запуске мимо формы (2026-07-27)
- Симптом: джоба создания отбивается 400 «укажите цель (goal_id)», хотя в UI поля «Счётчик»/«Цель» заполнены и фронтовый гард «Инвариант №1» их проверяет. Живой пример: `7fc7af30fff1` ушла без `goal_id` и отбилась; соседняя `4bce0676297a` прошла только потому, что `goal_id=586850590` был передан в payload вручную.
- Root-cause: запуск шёл **по API-пути, программно, минуя форму** — клиент просто не положил `goal_id` в тело `create_set_async`. Фронтовые гарды (`static/direct/automation_create.js:1370-1379` для «Создать набор РК» и `:2099-2107` для «Быстрых черновиков») этот путь физически не покрывают: они срабатывают ДО `set_plan` только при клике в UI. Т.е. отбой был не багом валидации, а её единственным местом — на самом дне, уже после постановки задачи.
- Где: гейт очереди `routes_jobs.py:161-172` (по `metrika_alert` из плана); расчёт алерта `create_set_plan.py:371 _metrika_alert_for`; нижняя страховка — `create_set_metrika.prepare_metrika` из `create_set_orchestrator.py:269-270`.
- Решение (2026-07-27): бэкенд-гейт трактуем как **защиту API-пути** и оставляем. Фронтовый гард НЕ ослабляем и НЕ удаляем — он даёт мгновенный фидбек без круга в сеть; UI-плашка `_metrikaAlertBanner` остаётся подстраховкой на достижимом из UI кейсе (счётчик принадлежит чужому аккаунту). Внешний клиент обязан передавать `counter_id`/`goal_id` — иначе 400 ДО `job_new`, осиротевшей задачи в очереди не будет.
- Детект-запрос (проверка гейта без живого создания): `POST /direct/api/set_plan` без `goal_id` → в ответе `metrika_alert.needed == true`; затем `POST /direct/api/create_set_async` с этим же `metrika_alert` → HTTP 400 и **ноль** новых строк в `direct_automation.direct_automation_jobs` (счётчик строк до/после совпадает).
- Статус: ✅ ПОДТВЕРЖДЕНО живым API-прогоном `69a140093e78` (2026-07-27): в `/direct/api/set_plan` с `counter_id=110881350`+`goal_id=586850590` ответ вернул `metrika_alert.needed=false`, тот же `metrika_alert` уехал в `create_set_async` → HTTP 200 и джоба стартовала. Отсутствие цели ловится на шаге плана, как и задумано.
- НЕ помогало ранее: полагаться на фронтовый гард `if(!_cnt||!_goal)` как на единственную защиту — API-путь его не проходит. ⚠️ Не путать с `METRIKA_MISSING_DISCOVERED_AT_CREATE` (ниже): там переносили ТОЧКУ проверки на шаг плана для UI-пути, здесь — про программный запуск мимо формы.

### METRIKA_MISSING_DISCOVERED_AT_CREATE — о нехватке счётчика/цели узнавали только при создании (2026-07-27)
- Симптом: пользователь считает план, генерит контент, жмёт «Создать» — и только там получает 400 «укажите счётчик Метрики» / «укажите цель (goal_id)» / «счётчик принадлежит аккаунту «X»». Время и генерация потрачены впустую.
- Root-cause: единственная валидация `create_set_metrika.prepare_metrika` звалась ТОЛЬКО из `create_set_orchestrator.py:269-270` (шаг СОЗДАНИЯ); `create_set_plan._set_plan_response` метрику не проверял вовсе.
- Где: `create_set_plan.py:371 _metrika_alert_for` (новая), вызов на `:454`, ключ `metrika_alert` в обоих ответах плана (`:787`, `:1524`); проводка коллбэков — `automation_runtime.py:1457-1459 _create_set_plan_deps`; гейт очереди — `routes_jobs.py:161-172`; UI — `static/direct/automation_create.js` (`_metrikaAlertBanner`, плашка + disabled «Создать»).
- Решение (2026-07-27): план зовёт ТУ ЖЕ `prepare_metrika` (вторую проверку рядом не писали) и отдаёт `metrika_alert {needed,error,counter_id,goal_id,metrika_note}`, ответ остаётся 200. Оба легальных случая сохранены: `via_cookie+no_cpa` → optional (needed=False + note), счётчик без цели → доподтягивание `goal_vse_formy`, алерт только если цели нет и после резолва. Гейт `create_set_async` отбивает запуск при `needed` ДО `job_new` (осиротевшая задача в очереди недопустима); подтверждения-обхода у метрики нет — только заполнить. Сбой Метрики/БД и отсутствие проводки коллбэков → `needed=False` (план не блокируем, гейт создания в оркестраторе остаётся).
- Верификация: `py_compile` + `node --check` OK; локально 68 passed (7 новых `tests/test_metrika_plan_alert.py`), LXC101 `/root/venv` 68 passed; живой резолв на LXC101: `porg-ozge4ntu` с пустой формой → counter 109986170 / goal 571275355, needed=False; несуществующий логин → needed=True «укажите счётчик Метрики»; он же с `via_cookie+n` → needed=False + note.
- Доработка (2026-07-27, ревью): ОБА fail-open теперь видимые. Ветка «коллбэки не проведены» (`create_set_plan.py:392-407`) пишет `direct.plan` WARNING с ИМЕНАМИ непроведённых коллбэков — один раз на процесс (модульный флаг `_METRIKA_DEPS_WARNED`, `set_plan` дёргается часто). Это важнее ветки сбоя Метрики: сбой Victory транзиентный, а обрыв проводки постоянный и выключал проверку метрики на шаге плана навсегда — молчаливая регрессия к «узнаём при создании».
- Статус: ✅ основная часть ПОДТВЕРЖДЕНА живым API-прогоном `69a140093e78` (2026-07-27): `/direct/api/set_plan` с `counter_id=110881350`+`goal_id=586850590` → `metrika_alert.needed=false`, тот же алерт уехал в `create_set_async` → HTTP 200, джоба стартовала; отсутствие цели ловится на шаге плана, а не при создании. 🟡 остаётся ОДНА непроверенная деталь — WARNING `direct.plan` про непроведённые коллбэки: он не воспроизводился живьём (коллбэки проведены, ветка не срабатывала).
- НЕ помогало ранее: — (первая правка этой сигнатуры). ⚠️ Не путать с `TP7_GOALID_FROM_PAYLOAD` (2026-07-11): там решался приоритет counter/goal из payload над FOREIGN-таблицей, сама точка проверки не переносилась.

### TP1_AUTOTARGET_INVERSION v2 — tp1-группы переведены на атомарный Grid (2026-07-27)
- Симптом (тот же, что у ❌-записи выше): 600 дефектных групп из 1325, 14 РК из 24 — `aon`→автотаргет ВЫКЛ, `aoff`→ВКЛ.
- Root-cause (доказан, не гипотеза): Phase 1.5 (`create_set_tp1_builders.py:392-448`, Grid `UpdateUnifiedAdGroups` после v501 `adgroups.add`) — **полный no-op**, отвечает `ok` и не меняет ничего. Доказательство: `713089308` (послали `isActive=True`) и `713089104` (послали `False`) дали ИДЕНТИЧНУЮ живую картину 84 ON / 29 SUSPENDED, тот же набор из 29 групп пофамильно. Посланное значение на результат не влияет.
- Контрдоказательство пути: tp2/tp4/tp5 создают группы через Grid `AddUnifiedAdGroups`, где `relevanceMatch` ставится ПРИ СОЗДАНИИ — 433 группы этим путём, **0 дефектов**; все 600 дефектов в 14 tp1-кампаниях v501-пути.
- Где: `create_set_tp1_builders.py:_build_tp1_adgroups` Фаза 1 (было: `tp5`→Grid, `tp1`→v501 `adgroups.add`) и Фаза 1.5.
- Решение (2026-07-27, вариант «а» Семёна): (1) Фаза 1 — ОДИН путь для tp1 и tp5: `gc.GridCreateClient.add_adgroups` + `gc.build_adgroup` (`grid_create_payloads.py:107-129`), `relevanceMatch.isActive=bool(autotarget)` атомарно при создании; выравнивание позиционного сдвига по имени сохранено. (2) Фаза 1.5 УДАЛЕНА целиком как неработающая. (3) Фаза 2 tp1 переведена с v5 `keywords.add` на Grid `AddKeywords` тем же клиентом — иначе Grid-группы + v5-ключи = смешанный транспорт и ключи-фантомы (`DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT`). (4) tp5: профиль `search_tp2` теперь идёт ВСЕГДА, независимо от планового флага — в поисковой кампании автотаргет выключить нельзя в принципе (доменный факт Семёна 2026-07-27); прежнее `search_tp2 if autotarget` давало дефолтные категории вместо `EXACT_V2_MARK/WITHOUT_BRAND` и было источником 4 живых WRONG_AUTOTARGET на tp5 `aoff`.
- Верификация: локально `.venv` 491 passed (11 тестов этой правки: isActive==флаг для aon/aoff, tp5 always-on, ключи aon=0 / aoff сохранены, псевдоключ не шлётся, сбой Grid → adgroups=0). Живой детект `.claude/sdd/detect_autotarget_tp1.py` — **после рестарта и прогона**, «было» = 600/1325.
- Доработка (2026-07-27, ревью коммита `d44236d1`, 2 Important): (1) **Фаза 4a осталась на v501.** Группа «все фиды» tp1 (`Товарная галерея · <фид>`) создавалась через `_v5_call("adgroups","add")` БЕЗ `relevanceMatch` → тот же дефолт ACTIVE даже в кампании планового `aoff`; детектор `grid_read.py:356-362` её не видит (нет токена `_aon_`/`_aoff_` в имени) → метрика «600 → 0» показала бы чисто при неверной группе. Переведена на `gc.build_adgroup(autotargeting=bool(autotarget))` + `_gcl.add_adgroups` (тот же клиент Фазы 1); UTM сохранён — `build_adgroup` кладёт `trackingParams = cmc.UTM_TEMPLATE`, тот же макрос, что `_UTM_TEMPLATE_TP1`. (2) **Тихий 0 ключей.** `GridCreateClient.add_keywords` (`grid_create.py:246-254`) при `validationResult.errors` НЕ бросает: печатает в stderr и отдаёт `[]` → `rep["keywords"]=0` при ПУСТОМ `rep["errors"]`. Добавлен гейт по образцу родного Grid-пути (`grid_create.py:591-599`): `kw_items>0` и `keywords==0` → запись `ключи(AddKeywords <tp>): 0 из N создано (валидатор Grid отклонил)`. Для tp5 она подхватывается `_synthesize_tp1_build_error` → singular error → `_cleanup_partial`; для **tp1 остаётся информационной** — это существующее протестированное решение (`tests/test_create_review_findings.py:124 test_tp1_no_keywords_does_not_set_singular_error`), менять его без решения Семёна нельзя.
- Статус: ✅ **ПОДТВЕРЖДЕНО живыми прогонами.** Короткий `fbb63cc8f962` (9 items): **0 дефектных групп из 216, 0 кампаний из 8** — и по v5-детектору, и по Grid `relevanceMatch.isActive`; tp1-ключи через Grid: build==live (33/4363, 33/0, 33/4363, 8/150). Полные `3f56db987ab9` и `96f76846fc68` (26 items): **0 дефектных из 1325 групп / 0 из 24 кампаний** (база — 600/1325 и 14/24). Боевой прогон 4 аккаунтов 2026-07-28 (`porg-xjxpfxby`/`rgwzgo57`/`azsw6eyh`/`4ealp4ry`): `wrong_autotarget_groups=0`, 0 дефектных из 12/10/10/10 РК. Локально `direct/tests` 494 passed, 3 pre-existing fail (copy_rename ×2, m3_lock — падают и без правки).
- НЕ помогало ранее: (1) псевдоключ `---autotargeting` в `kw_items` — легаси-представление, не механизм управления; (2) Phase 1.5 `UpdateUnifiedAdGroups` поверх свежих v501-групп — доказанный no-op, не «лаг репликации»; (3) ❌ ЛОЖНАЯ ЗАДАЧА: «tp5 хардкодит `_aon_` в имени» — НЕ баг, `_aon_` для tp5 единственное корректное значение; `create_set_tp1_builders.py:87-95` менять нельзя.
- ⚠️ ВАЖНО для диагностики (боевой прогон 4 аккаунтов 2026-07-28): **`WRONG_AUTOTARGET` в in-job `live_verification` — НЕ приговор.** На `porg-xjxpfxby` (kuderko/С пробегом) джоба `2d71d6163b80` финишировала с `live_verification=fail, errors=12` (12 tp2/tp4 РК), на `porg-rgwzgo57` — `errors=6`. После отложенной авто-добивки (`direct_delayed_repairs`, старт ~180 с после `done`) живой перезамер тем же ридером, что и верификатор (`GridReadClient.campaign_content_counts`, критерий `grid_read.py:351`) дал **`wrong_autotarget_groups=0` на всех 4 аккаунтах** (12/10/10/10 tp2/4/5 РК). Причём у `rgwzgo57` репейр отчитался «исполнено 0, остаток 0» — дефект снялся САМ, т.е. это лаг реплики Grid между созданием и in-job проверкой, а не недокрут. Вывод: судить по `live_verification` внутри джобы нельзя, мерить надо ПОСЛЕ delayed repair.

### TP5_MODEL_HREF_ROOT — tp5 «Товарная галерея - Модели» per-model group ведёт на корень домена (2026-07-27)
- Симптом: кампания tp5_cpc_site «Товарная галерея - Модели», группа «Lada Niva Legend» (кодер ct0186_aon_n000_r0088_ct010_ag011_g00), комбинаторное объявление — «Ссылка в объявлении» = `https://newautos-193.site` (корень), ожидаемо `/auto/lada/niva-legend`. job 446ab5bd0ab3, porg-pl6iavd5.
- Root-cause: `_valid_pack_brand_name("ct0000", ...)` возвращает `""` (ct0000 = Общее). В per-model `_multi`-ветке (ct0000 + `_gk`/`_uname`) `brand=""` → `_pack_group_href(ct0000, "", ...)` → `_model_page_href(root, type, "")` → голый домен. Имя `_uname` («Lada Niva Legend») корректно шло в кодер группы и ключи, но НЕ в href.
- Где: `create_set_tp1_builders.py:1044-1045, 1086-1088` (v5 путь `_build_tp1_from_pack`) и `:2146-2147, 2188-2190` (cookie/grid путь `_tp1_pack_groups`).
- Решение (2026-07-27): оба пути + их pre-pass: для per-model группы (`_multi and _uname` для v5, `_gk and _uname` для cookie/grid) строим href напрямую через `_model_page_href(_site_root_href(href), site_type, _uname)`, минуя `_pack_group_href` (feed-lookup с ct0000 даёт "Марки" по умолчанию → brand-level URL). Стандартные Общее-группы с `_multi=False`/`_gk=""` — без изменений (корень домена штатен). `py_compile` OK, 9/9 href-related тестов passed, 414 всего passed (20 pre-existing failures несвязанных).
- Статус: ⚠️ ЧАСТИЧНО (живой прогон `69a140093e78`, 2026-07-27). v5 `ads.get(TEXT_AD, Href)` по 24 кампаниям: 1325 объявлений, корневых `https://newautos-193.site` — 18. tp1 (14 кампаний, 707 объявлений) — **0 корневых**, т.е. tp1-ветка фикса держит. Но `tp5 «Товарная галерея - Lada - Модели»` (713088812) всё ещё даёт **1** корневое из 6, а tp2/tp4 (`AD_HREF_ROOT_INSTEAD_OF_MODEL`, вне охвата этого фикса) — 3+3+8+3=17. Остаток нужно чинить отдельно: детектор джобы поднял ровно 5 кампаний с теми же числами.
- НЕ помогало ранее: link_check.py не виноват — fail-open по таймауту сохраняет ИСХОДНЫЙ url (гипотеза проверена и отвергнута).

### TP1_AUTOTARGET_INVERSION — инверсия relevanceMatch.isActive для aon/aoff в tp1 РСЯ (2026-07-27)
- Симптом: Случай A — группа с кодером `aon` (ct0146, camp 713080285, grp 5777189791) → UI автотаргетинг ВЫКЛ (ждали ВКЛ). Случай B — группа с кодером `aoff` (ct0301, camp 713080436, grp 5777191390) → UI автотаргетинг ВКЛ (ждали ВЫКЛ). Job 446ab5bd0ab3, аккаунт porg-pl6iavd5, scherbakova, Мультибренд.
- Root-cause: `v501 adgroups.add` (Phase 1 tp1) не поддерживает `relevanceMatch` → Яндекс ставит дефолт ACTIVE. Для `aon` вместо явного `isActive=True` применялся псевдоключ `"---autotargeting"` (legasi-путь), который уводил группу в легаси-режим с `isActive=False` → инверсия. Для `aoff` псевдоключ не добавлялся, но и явного `isActive=False` не было → дефолт Яндекса ACTIVE → инверсия. Правильный field: `relevanceMatch.isActive` (объект группы, читается `grid_read.py:335`).
- Где: `create_set_tp1_builders.py:352-402` (Phase 1.5, новый) + бывшие строки 388-392 (Phase 2, `if autotarget: kw_items.append(_AUTOTARGET_KW)`).
- Решение (2026-07-27): (1) **Новая Phase 1.5** (после v5 adgroups.add, перед keywords.add): Grid `UpdateUnifiedAdGroups` явно задаёт `relevanceMatch.isActive=bool(autotarget)` для всех созданных групп. Структура для `aon` — полный набор 5 категорий + 3 бренда, для `aoff` — пустые категории, `isActive=False`. (2) **Удалён псевдоключ** `"---autotargeting"` из Phase 2: `if autotarget: kw_items.append(_AUTOTARGET_KW)` убрано целиком. Реальные ключи по-прежнему добавляются при `keep_keywords=True`. (3) 4 unit-теста в `test_create_auto_regressions.py`: aon→isActive=True, aoff→isActive=False, нет псевдоключа в keywords.add, сохранение реальных ключей.
- Статус: ❌ НЕ ПОМОГЛО — опровергнуто живым прогоном `69a140093e78` (porg-pl6iavd5, scherbakova/Мультибренд, 2026-07-27 18:15→19:00 +05, код рестартован 18:08:33). Grid `groups_for_edit → relevance_match.isActive` против планового `autotarget`: расходится **18 из 24** кампаний. Все 14 tp1: `aon`-кампании (713089220, 713089560, 713089806, 713089963) — **все** группы `isActive=False` (113/113, 113/113, 33/33, 33/33), т.е. Phase 1.5 не включила автотаргет; `aoff`-кампании (713089104, 713089461, 713089731, 713089916, 713090032, 713090068) — `True` у части групп (84/113, 84/113, 28/33, 28/33, 8/8, 8/8), т.е. не выключила; `aon+kw` — тот же расклад 84 True/29 False. Плюс 4 tp5 `aoff` (713088799/713088812/713089044/713089072) — 100% групп `True`. Совпало только 6/24 (tp2×2, tp4×2, tp5 `aon`×2). Верификатор джобы независимо дал `WRONG_AUTOTARGET × 18`. Характерный паттерн 84/29 и 28/5 (а не 113/0) намекает: `UpdateUnifiedAdGroups` Фазы 1.5 применяется лишь к части групп (похоже на лаг репликации свежих v501-групп — тот самый риск из «НЕ помогло ранее»). Псевдоключ `---autotargeting` при этом виден в v5 `keywords.get` у ВСЕХ 1325 групп — это легаси-представление, а не источник истины; судить по нему нельзя. Доработка 2026-07-27 (вердикт ❌ проверяющего): (1) Phase 1.5 exception теперь проставляет rep["error"] (singular) → _cleanup_partial сносит черновик вместо ok=True с неверным isActive. (2) добавлен комментарий legacy-страховки _AUTOTARGET_KW. (3) grid_read.py: новый счётчик wrong_autotarget_rsya_groups для tp1 (aon/aoff vs isActive). (4) grid_content_verifier.py: tp1-ветка WRONG_AUTOTARGET по wrong_autotarget_rsya_groups.
- НЕ помогало ранее: псевдоключ `"---autotargeting"` (легаси-механизм) — он и был причиной инверсии для aon.

### IMAGES_REPAIR_CONTENT_GAP_LOOP_NO_TERMINAL — images_repair зависал в цикле на content-gap ct без IMAGE_NO_POOL маркера (Баг A, 2026-07-22)
- Симптом: job `fe6491ee06c3` (porg-pl6iavd5): `auto_repair_full.executed` содержит 12 одинаковых записей `action=images_repair`, все с note «контент-гэп: нет креативов для ct ['ct0067']». 2 итерации × 6 кампаний без прогресса → watchdog убил через 30 мин с `status='failed'`, `remaining_actions=6`.
- Root-cause: `_campaign_images_repair` при чистом content-gap (нет путей в Manual/<ct> или паке) возвращала `ok=True, skipped_content_gap=True, ads_updated=0`. `_run_per_campaign_repair` считала её "repaired" (ok=True). `execute_all_in_place` видела status=200 → добавляла в `executed` (не в `failed`). Счётчик `executed>0` → anti-pingpong check (`if not res.get("executed")`) НЕ срабатывал → цикл шёл на следующую итерацию → та же картина. После двух итераций (`_DELAYED_FULL_REPAIR_MAX_ITERATIONS=2`) — финальная `_live_plan()` видела IMAGE_MISSING → remaining>0 → `final_status="partial"` → reschedule.
- Где: `repair_media.py::execute_images_repair` — не детектировал all-gap как терминальный. `queue_server.py::_repair_failures_nonfixable` — не знал о image_no_pool.
- Решение (2026-07-22, коммит `869993b8`): `execute_images_repair` после вызова `_run_per_campaign_repair` проверяет: если `all(r.get("skipped_content_gap") for r in results)` и нет `upload_fail_cts` → `out["image_no_pool"]=True, status=207`. Status 207 → в `execute_all_in_place` идёт в `failed_actions`, не в `executed` → `executed=0` → anti-pingpong → `_repair_failures_nonfixable` → `image_no_pool=True` → `continue` (nonfixable) → `_nonfixable_stop=True` → reschedule отменён. Ретраебл-ошибки Grid (upload_fail_cts) маркер НЕ получают.
- Верификация: `py_compile repair_media.py queue_server.py` — OK.
- Статус: 🟡 фикс на Mac. Ждёт деплоя и живого прогона на job с content-gap ct.
- НЕ помогло ранее: фикс 2026-07-13 (ok=True вместо hard-fail) убрал ложный ok=False, но НЕ добавил терминальный маркер → loop продолжал переставлять то же действие.

### CONTENT_REPAIR_WATCHDOG_KILL_NO_REQUEUE — после watchdog-убийства content_repair с остатком действий не создавалось реквью (Баг B, 2026-07-22)
- Симптом: job `fe6491ee06c3` watchdog убил с `status='failed'`, `note='watchdog: stuck running >30 мин'`. После убийства ни одного нового delayed_repair для login `porg-pl6iavd5` не создалось — остаток действий (FEED_FILTER_MISSING_GRID, GROUP_COUNT_BELOW_SLEPOK, FOREIGN_MODEL_KEYWORDS и т.п.) потерян навсегда.
- Root-cause: watchdog-handler (К1, `queue_server.py::_delayed_repair_daemon_loop`) при обнаружении stuck content_repair: (1) помечал строку failed, (2) закрывал child dcr:{did} через `_parent_absorb_child_progress`. Шага «создать новый реквью» не было. Строка оставалась в `status='failed'` навсегда.
- Где: `queue_server.py::_delayed_repair_daemon_loop` — блок `if _wd_failed_content` (~1386-1397).
- Решение (2026-07-22, коммит `869993b8`, тот же что Баг A): добавлены `_DELAYED_REPAIR_WATCHDOG_REQUEUE_MAX=2` и функция `_delayed_content_repair_requeue_after_watchdog(did)`. После `_parent_absorb_child_progress` в watchdog-loop вызывается реквью best-effort: UPDATE строки (не INSERT — та же строка) `status='waiting', attempts+1, run_at+300с` если `attempts < 2`. Кап 2 страхует от вечного цикла если Баг A не перехватил все нечинимые случаи. Бэкофф 300с = `_DELAYED_CONTENT_REPAIR_DELAY_SECONDS`.
- Верификация: `py_compile queue_server.py` — OK.
- Статус: 🟡 фикс на Mac. Ждёт деплоя и живого инцидента watchdog-kill content_repair.
- НЕ помогло ранее: (первый фикс по этой сигнатуре).

### TP7_SINGLE_FEED_PLAN_GATE_DROPS_PRODUCT — tp7 не создаётся при неподтверждённом профильном фиде (2026-07-22)
- Симптом: пользователь выбрал tp7 в UI (превью «8 камп × фиды»), но job создал 0 tp7-кампаний. tp5/tp1-товарка при том же прогоне выжили.
- Где: `create_set_plan.py:531` — `feeds = []` в ветке `else` когда `single_feed=True` + `feed_confirmed=False` + strict-lookup не нашёл профильный фид. `_emit_struct` fan-out по `feeds` → `feeds=[]` → 0 product-items → body.items без tp7.
- Root-cause: strict-lookup `_first_url_feed(strict=True)` на ПЛАНЕ мог транзиентно провалиться (API/152 нет баллов) → `feeds=[]` → tp7 убит из плана навсегда. tp5/tp3 выживали, потому что их фид резолвится на БИЛДЕ (`_resolve_single_feed_variants` в `create_set_feed_builders.py:917`) — повторный lookup на билде проходил успешно.
- Решение (2026-07-22): вместо `feeds = []` ставим sentinel `feeds = [{"id": 0, "name": None, "url": ""}]`. `_emit_struct` создаёт по 1 plan-item на позицию с `feed_id=0`. На билде (`create_set_master_product.py:946`) при `it_feed=None` + `single_feed=True` выполняем повторный strict-lookup: нашли → `it_feed = profile_id`, нет → skip (аналог tp5). Добавлен `_first_url_feed` в `_master_product_deps` (`automation_runtime.py:3237`).
- Статус: ✅ ПОДТВЕРЖДЕНО живым прогоном `69a140093e78` (scherbakova/Мультибренд, `single_feed=true`, 2026-07-27): план вернул `feeds=1` (`/yandex.xml`), `feed_alert.needed=false` и **2 product-item'а** tp7 (ct0000 + ct0111) — нулевого tp7 больше нет. ct0000 создана (id 713088540, `feed_id=listings_feed_id=3593963`), ct0111 отбита отдельной причиной `CONTENT_GAP_IMAGES_LOW` (4 картинки < 5), не фидовой.
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### GRID_UPDATE_CAMPAIGNS_CONTEXT_LIMIT_REMOVED — contextLimit убран из GdUpdateUnifiedCampaignInput (2026-07-22)
- Симптом: весь finalize для tp1/tp5 (grid_finalize.py) и tp2/tp4 (create_set_finalize.py) падает с «field contextLimit not defined for input GdUpdateUnifiedCampaignInput». Sitelinks, callouts, disabledPlaces, bid_modifiers НЕ применяются. rsya_finalized=false у всех 21 кампаний.
- Где: `grid_uc_template.json:7` (ключ `"contextLimit": 100` в шаблоне UpdateCampaigns); `grid_finalize.py:861` (`_unified_campaign_update_from_edit_row` — для copy-операций).
- Root-cause: Яндекс убрал поле `contextLimit` из схемы `GdUpdateUnifiedCampaignInput`. Шаблон `grid_uc_template.json` его содержал → deepcopy шаблона в finalize всегда отправлял это поле → вся мутация UpdateCampaigns отклонялась (не частично — целиком). `grid_create_payloads.py:72` (AddCampaigns) поле оставлено — иной input-тип, campaigns создаются успешно.
- Решение (2026-07-22): удалено `"contextLimit": 100,` из `grid_uc_template.json` и из `_unified_campaign_update_from_edit_row` в `grid_finalize.py`. `grid_create_payloads.py` не тронут — AddCampaigns поле принимает.
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя и живого прогона.
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### TP1_SHOPPING_COMBI_WRONG_AD_COUNT — «Комби» (без Фид) создавала 3 объявления вместо 1 (2026-07-22)
- Симптом: tp1 кампания «Комби» (tp1_catalog=None) для scherbakova получала ShoppingAd+ListingAd в дополнение к TextAd (3 объявления). Правило: «Комби+Фид → 3 объявления, Комби → 1 объявление».
- Где: `create_set_tp1.py:76` — `tp1_shopping = slepok_uses_shopping(slepok, "tp1") or tp1_products_only or bool(it.get("tp1_catalog"))`.
- Root-cause: `_SHOPPING_RULE = {"tp1": {"scherbakova"}}` делал `slepok_uses_shopping("scherbakova","tp1")=True`, что форсило shopping на ВСЕ tp1 кампании слепка, перекрывая per-РК флаг `tp1_catalog`. OR-цепочка не доходила до `tp1_catalog`.
- Решение (2026-07-22): строка 76 изменена на `tp1_shopping = tp1_products_only or bool(it.get("tp1_catalog"))`. Shopping теперь только при явном per-кампания tp1_catalog. `_SHOPPING_RULE["tp1"]` и функция `_slepok_uses_shopping` не удалялись.
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя и живого прогона (шербакова tp1: Комби → 1 объявление, Комби+Фид → 3 объявления).
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### TP1_CAMPAIGN_MINUS_WRONG — глобальные минус-слова («отзывы») ставились на tp1 campaign-level (2026-07-22)
- Симптом: tp1 РСЯ кампании получают «отзывы» (из `direct_global_minus_words`, ct='*') через v5 campaigns.update NegativeKeywords, хотя по правилу для tp1 campaign-level минус-фразы не выставляются.
- Где: `create_set_tp1_builders.py:1319` — вызов `_apply_campaign_direct_minus(..., "tp1", ...)`.
- Root-cause: вызов существовал «для всех режимов» согласно старому комментарию; правило Семёна «tp1 минус-слова не ставим» не было закодировано. `_apply_campaign_direct_minus` уже имеет гейт `tp_code != "tp1"` для ПАКОВЫХ минусов, но ГЛОБАЛЬНЫЕ слова (строка 410) он ставит всегда при любом tp_code.
- Решение (2026-07-22): вызов `_apply_campaign_direct_minus` для tp1 убран из `create_set_tp1_builders.py:1319`. `_minus_note = None` (no-op). Для других tp (tp2/tp4/tp5 в `create_set_feed_builders.py`) вызов не тронут.
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя и живого прогона.
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### GRID_FINALIZE_ABORTS_V5_CORRECTIONS — падение Grid finalize прерывало v5 _apply_corrections (2026-07-22)
- Симптом: tp2 Grid finalize падал (contextLimit или иная ошибка UpdateCampaigns) → exception поглощался в try/except → `_apply_corrections` (v5 REST, логически независимый) не выполнялся → корректировки tp2 пустые.
- Где: `create_set_feed_builders.py:426-474` — `_apply_corrections` (строка 469) стоял в одном try/except с `_finalize_search_via_grid` (строка 455).
- Root-cause: оба вызова в одном try — исключение из Grid finalize обрывало всё, включая v5 API.
- Решение (2026-07-22): `_apply_corrections` вынесен из основного try/except в отдельный try/except ПОСЛЕ except-блока (defense-in-depth; аналогично существующему паттерну `_apply_campaign_direct_minus`). Логика самих корректировок не изменялась.
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя и живого прогона.
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### POSEVY_DELETE_DRAFTS_UAC_WRONG_BRANCH — «Удалить черновики» не удаляет GdPostCampaign (2026-07-22)
- Симптом: кнопка «Удалить черновики» нажата → посевные кампании tp8/tp9/tp10 (typename=GdPostCampaign) остаются в кабинете со статусом DRAFT. Воспроизведено на porg-uy3huxcn (camp 712963473/490/499).
- Где: `account_service.py:_delete_drafts_core` — роутинг удаления ~строки 201-244 (до патча), ветка `else:` (UAC).
- Root-cause: в роутинге `if tn=="GdUnifiedCampaign"` → v5-delete, `else` → `uac.delete_campaign()`. GdPostCampaign — не ЕПК и не UAC, проваливался в `else`, UAC-DELETE для посева либо игнорировался тихо, либо падал, после чего `_grid_delete_one` fallback тоже не срабатывал достаточно надёжно (re-raise uac_err поглощал попытку). Итог: посевы «удалены» по счётчику, но живы в кабинете.
- Решение (2026-07-22): добавлена явная ветка `elif tn == "GdPostCampaign":` перед `else:` — напрямую вызывает `_grid_delete_one(login, cid)` (Grid `deleteCampaigns` по куке) + `_grid_draft_contains` fallback, минуя UAC-эндпоинт. py_compile OK.
- Статус: 🟡 фикс на Mac, ждёт деплоя и live-проверки (посевы 712963473/490/499 на porg-uy3huxcn).
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### LINK_CHECK_404_FALLBACK — href объявления ведёт на 404 (поколение/кузов убраны из каталога) (2026-07-22)
- Симптом: href группы (`model_href`) — глубокий feed-URL типа `https://site/auto/renault/sandero/ii/hatchback-5d` — ведёт на 404, т.к. данная конфигурация убрана из каталога дилера, хотя в фиде фид-URL сохранился.
- Где: `create_set_text_builders.py:_build_text_from_pack` (покрывает tp2/tp4). Аналогичные точки в `create_set_tp1_builders.py:_build_tp1_from_pack` и `_tp1_pack_groups` — покрыты коммитом `a65321c`. Также: `create_set_master_product.py:248` (tp6/tp7, коммит `d4f77af`) — там один it_href на кампанию, батчинг не нужен.
- Root-cause: feed-URL берётся «как есть» из Яндекс-фида через `_account_offer_urls`→`_feed_url_for_model`; фид может содержать устаревшие глубокие пути (`/sandero/ii/hatchback-5d`), которые 404 в live кабинете.
- Решение (2026-07-22): новый модуль `link_check.py::resolve_or_fallback_url(url, timeout=3.0)`. HEAD-запрос к каждому URL с путём; 404 → откусить последний сегмент и повторить; первый не-404 (2xx/3xx) — использовать вместо оригинала. Таймаут/5xx → исходный URL без изменений (сайт может быть временно недоступен). Кэш module-level 60 мин × 2000 ключей (потокобезопасный). Голый домен — не проверяется. Вызов добавлен в `create_set_text_builders.py:472` после определения `model_href`.
- Статус: 🟡 покрыто во всех builder'ах: tp2/tp4 (`create_set_text_builders.py`, коммит `268d30f`→текущий), tp1/tp5 (`create_set_tp1_builders.py`, коммит `a65321c`→текущий). Параллельный pre-pass через `resolve_urls_batch` подключён в 3/3 местах (helper `_pack_group_href`, единая логика в pre-pass и основном цикле). tp6/tp7 (`create_set_master_product.py:248`, коммит `d4f77af`) — single-href, батч не нужен. Ждёт деплоя и живого прогона.
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### POSEVY_STRICT_SLEPOK_GATE_SILENT_DROP — посевы tp8/9/10 молча отбрасывались гейтом targeting_profile (2026-07-21)
- Симптом: джоба `113fdfba02e2` (agent=pavlov, site_type=Мультибренд) подала 3 посевных пункта (post_tp8/tp9/tp10) → 0 кампаний создано, 0 result-строк записано, UI показал «Проверка: пройдена / Добивка не требуется» (ложный успех).
- Где: `create_set_orchestrator.py` ~719-724 — гейт `_slepok_profile_excludes_tp(agent, eff_site, _it_tp)`.
- Root-cause (Bug 1): `_TYPE_TO_TP["post_tp8"]="tp8"` → гейт проверял `pavlov/Мультибренд` по `targeting_profile.json` → в профиле только `[tp1,tp2,tp5,tp6,tp7]` (посевные tp8/9/10 НАМЕРЕННО не включаются — их структура универсальна, независима от профиля). Гейт отбрасывал все 3 пункта с `_bi(_job); return` без записи result-строки → тихая потеря.
- Root-cause (Bug 2): при исключении гейтом не добавлялась result-строка → `len(results)==0` → счётчик «0 из 0» маскировал потерю; live_verification видела «ничего не создано» и возвращала pass; repair_gate — «нечего чинить» → UI суммировал как «пройдена».
- Решение (2026-07-21): `create_set_orchestrator.py` 719-727:
  1. (Bug 1) добавлен флаг `_is_posevy = it.get("type") in ("post_tp8","post_tp9","post_tp10")`, условие гейта обёрнуто в `not _is_posevy and ...` → посевы обходят гейт полностью.
  2. (Bug 2) при исключении гейтом теперь добавляется `ch.results.append({"ok": False, "name": name, "skipped": True, "error": "excluded by targeting_profile gate: ..."})` → len(results) == len(items) всегда.
  3. UI `automation_jobs.js` 167-169: статус верификации теперь берётся как «худший» из live_verification.status и verification.status (по ранку fail>warn>pass) → static "fail" не маскируется live "pass"; counts берутся из того, у кого больше.
- py_compile: OK. node --check: OK.
- Статус: 🟡 фикс на Mac, ждёт деплоя и живого прогона (3/3 посевных созданы + счётчик 3/3 + Проверка: нужна добивка или пройдена — НЕ «пройдена при 0 результатах»).
- НЕ помогало ранее: (первый фикс по этой сигнатуре).

### TP5_KEYWORDS_LOST_MIXED_TRANSPORT — ключи tp5 не закрепляются (смешанный транспорт Grid+v5) (2026-07-21)
- Симптом: tp5 «Поиск + ТГ» кампании после создания имеют 0 ключей в живом кабинете (LIVE=0), хотя build отчитался о создании (напр. 340/283/2265 ключей). Частичная потеря тоже возможна (17/29 групп без ключей).
- Где: `create_set_tp1_builders.py::_build_tp1_adgroups` Фаза 2 (~строки 307-350), `tp_code=="tp5"`.
- Root-cause: тот же класс бага, что `DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT` (ERRORS_JOURNAL ~строка 3127). Фаза 1 создаёт группы через Grid `AddUnifiedAdGroups` (_gcl5), Фаза 2 заливала ключи через v5 `keywords.add` на этих СВЕЖИХ Grid-группах. Лаг репликации Grid→v5 → v5 рапортует Id (нет ошибки), но ключи физически не закрепляются → LIVE=0 или частичные. Баг воспроизводится на любом tp5-прогоне, не только в докрутке.
- Доказательство: live-чтение Grid кабинет porg-dmwfp3dk после build: 143 поисковые группы без ключей (3/4 КС-кампаний); wrong_autotarget=0 (автотаргет ОК, это не WRONG_AUTOTARGET).
- Решение (2026-07-21): `create_set_tp1_builders.py` Фаза 2 — для `tp_code=="tp5"` ключи льются через `_gcl5.add_keywords(kw_g5)` (тот же Grid-клиент/транспорт, что создал группы в Фазе 1); v5-путь остался только для tp1 РСЯ. `gc.unique_keyword_ids` считает РАЗНЫХ keywordId (аналог v5-пути). Эталон: `create_set_text_builders.py:96-119` (tp2/tp4).
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя + живого прогона tp5.
- НЕ помогало ранее: v5-путь заливки ключей на Grid-группах (именно это и является дефектом). Смешивать транспорты (Grid группы + v5 ключи) — НЕ повторять для tp2/tp4/tp5.

### FOREIGN_MODEL_KEYWORDS_CT_LEVEL_FALSEPOS — ложное срабатывание FOREIGN_MODEL_KEYWORDS в аудите (рассинхрон create/audit для «С пробегом») (2026-07-21)
- Симптом: DOD-аудит флагает FOREIGN_MODEL_KEYWORDS на легитимных брендовых группах слепков site_type «С пробегом». Аудит видит «ключи чужой марки» там, где их нет — группа ct0000 «Renault Duster» (или другие бренды) корректно несёт свои ключи, но аудит определяет «свою модель» по ct-уровню (ct0000 → одна марка), а не по имени группы.
- Где: `campaign_spec_audit.py::_audit_search_keywords`, ветки FOREIGN_MODEL_KEYWORDS (~строки 303-397).
- Root-cause: рассинхрон create/audit. Create-сторона (`create_set_text_builders.py:496-502`, `_filter_group_keywords(... model=_uname)`) использует модель КОНКРЕТНОЙ группы из display-суффикса (после « — »). Аудит использовал `ct_name_fm.get(own_ct) / ct_model_fm.get(own_ct)` — ct-уровневый справочник, который для «С пробегом» ct0000 даёт одну марку на 25+ брендовых групп → ложные «чужемодельные» ключи. Плюс `_mst` не срезал минус-слова в ключе → собственный минус в ключе группы мог выглядеть как токен чужой модели.
- Решение (2026-07-21):
  1. В ветке «Модели»: `brand_fm` берётся из display-суффикса имени группы (после « — »), фолбэк на ct-справочник только при отсутствии суффикса.
  2. В обеих ветках: `_mst` заменён на `_kw_positive_tokens` (`_kwpt`) — срезает минус-слова ключа перед токенизацией.
  3. В ветке «Общее»: если display-суффикс группы содержит токены известных марок (`all_brand_toks`) → `continue` (брендовая группа под общим ct, не флагуем).
  4. Добавлен импорт `_kw_positive_tokens as _kwpt` из `text_gen`.
- Статус: 🟡 фикс на Mac, py_compile OK. Ждёт деплоя.
- НЕ помогало ранее: ct-уровневый справочник (это и есть корень ложняка).

### FIX_A_PLAN_NAME_REBUILD_DUPLICATE — Fix A пересобирал уже готовые план-имена tp6/tp7 → дубль «Автотаргетинг» (2026-07-21)
- Симптом: живая tp7-кампания №712935217 названа «ТК - Автотаргетинг - Общая - КС + Автотаргетинг - Республика Башкортостан» — слово «Автотаргетинг» дважды.
- Где: `create_set_master_product.py:887` — гард «Fix A» (`re.match(r'^tp[67]_cp[ac]_(site|kviz)_ct\d+_a(?:on|off)_', name)`), задуманный ловить ТОЛЬКО сырой структурный слаг.
- Root-cause: regex якорится в начало строки, а готовое план-имя (`_build_name`) ТОЖЕ начинается с тех же кодов (` — ` идёт уже после префикса) → матчил ВСЕГДА, не только на сыром слаге, вопреки собственному комментарию «красивое имя не матчит». Fix A выбрасывал уже чистое имя плана и пересобирал его из недедуплицированного `display` (`_slepok_struct_groups`: `group.name + " - " + item.t` без снятия пересечений).
- Решение: добавлено `and " — " not in name` — план-имена (всегда содержат длинное тире) больше не попадают под пересборку.
- Масштаб риска: 46 позиций tp6/tp7 в 8 слепках (pavlov, terehov, scherbakova, zubakin, salamahin, chepelev, karavaev, tumashenko).
- Статус: ✅ фикс + 117 тестов + независимая проверка (`direct_verifier`), коммит `3d71fef`. Ждёт деплоя.

### BUTTON_404_GENERIC_AVTO — кнопка «Получить скидку»/href объявления вели на несуществующую «/auto/avto» (2026-07-21)
- Симптом: живой href `https://multicars-ufa.site/auto/avto` — 404, страницы никогда не существовало.
- Где: `create_set_text_builders.py:447` (ветка авто-слепков без структурных имён) → `model_urls.py:_model_page_href:155-163`.
- Root-cause: при отсутствии реального ct-бренда `_valid_pack_brand_name` корректно возвращал "" — но код тут же подставлял литеральный фолбэк `"Авто"` (нужен только для ТЕКСТА объявления). Этот же `brand` без разбора уходил в `_model_page_href(href, site_type, brand)`, когда у ct нет реального URL из фида — а формула трактует ЛЮБОЕ однословное имя как марку: `_slugify("Авто")="avto"` → `/auto/avto`.
- Решение: формульный deep-link зовётся ТОЛЬКО с подтверждённым РЕАЛЬНЫМ брендом (`_real_brand`, значение до фолбэка); при его отсутствии — на главную сайта. Литеральный «Авто» остаётся в тексте объявления, просто не течёт в URL.
- Live-репэйр: 2 объявления в porg-ln7tz7xh (ad_href+btn_href) исправлены на главную; porg-5wvv6vff — брака не найдено.
- Статус: ✅ код-фикс + live-репэйр + независимая проверка, коммит `b3ccf0d`. Ждёт деплоя.

### CREDIT_AMOUNT_MISMATCH_UTP — разные суммы кредита в заголовках одного набора объявлений (2026-07-21)
- Симптом: в одной группе заголовков одновременно «Автокредит от 12 000₽/мес» и «...от 9 000₽/мес».
- Где: `create_content.py`, `run_gen_campaign_content`.
- Root-cause: `assemble_campaign` (ai_agents.py) вызывает `unify_utp_numbers` на СВОЕЙ локальной копии titles/texts — но после него `good_t`/`good_x` (ненормализованные) ещё трижды подмешиваются обратно через `_merge_lines` (topup из live-seed, из LLM, из judge-регенерации). `_merge_lines` дедуплицирует по точному совпадению строки → нормализованный и исходный варианты одного заголовка проходят как РАЗНЫЕ строки.
- Решение: добавлен терминальный вызов `unify_utp_numbers` перед итоговым `return`, ПОСЛЕ topup И после UTP-судьи — на итоговом содержимом, не на локальной копии.
- Статус: ✅ код-фикс, 117 тестов, коммит `3ada8dd`. Не чинит уже созданные живые РК — для live нужен `content_repair`. Ждёт деплоя.

### COMBI_FID_TEXTAD_MISSING — «Комби+Фид» создавалась без TextAd (только ShoppingAd+ListingAd) (2026-07-21)
- Симптом: tp1-кампания с именем «Комби+Фид» (напр. «РСЯ - Комби+Фид - Модели - КС») создавалась только с ShoppingAd+ListingAd, без TextAd (комбинированного объявления). По CODER.md ct010_ag011 = TextAd+ShoppingAd+ListingAd.
- Масштаб: 228 «Комби+Фид»-позиций во всех 13 слепках — все теряли TextAd.
- Где: `create_set_plan.py:689` (вычисление `_prod_only`) → `create_set_tp1.py:69,76` (`tp1_products_only`) → `create_set_tp1_builders.py:1055,365` (`_skip_text_ads = products_only`; `if products_only: break` — TextAd-цикл прерывается).
- Root-cause: `_prod_only = ("фид" in _low_cn) or ...` срабатывал на имени «Комби+**Фид**», ставил `products_only=True` — хотя это не чистый фид, а комбинированная кампания с TextAd.
- Решение (2026-07-21): `create_set_plan.py:686-710` — добавлены `_is_combi = "комби" in _low_cn` и `_has_feed_or_smart`; `_prod_only` теперь `(not _is_combi) and _has_feed_or_smart`. Внутри `_emit_tp1`: если `_is_combi and _has_feed_or_smart` → `tp1_catalog=True` (форсит ShoppingAd+ListingAd без `products_only`, TextAd сохраняется). Чистые «Фид»/«Смарт-Баннер» (без «комби») остаются `products_only=True`.
- Статус: 🟡 py_compile OK, 69 тестов OK, функциональная проверка 228 combi=OK / 64 pure_fid=OK. Ждёт деплоя + живого прогона.
- НЕ помогло ранее: `_slepok_uses_shopping` — не источник (вчерашняя гипотеза, неверная).

### TP6_MANUAL_AGE_THRESHOLD_35_TO_25 — порог возраста tp6-ручных изменён с 35+ на 25+ (2026-07-21)
- Причина изменения (требование Семёна 2026-07-21): исключать только брекет 18-24 → socdem стартует с 25+.
- Было: `age_lower="age_35"` (35+, исключало ОБА брекета 18-24 И 25-34), установлено DoD §3.6 / 2026-07-09.
- Стало: `age_lower="age_25"` (25+, исключает только 18-24). Автотаргет (`age_18`) и tp7 (`age_18`) не тронуты.
- Файлы: `create_set_master_product.py` (age_lower payload), `detect_tp67_name_socdem.py` (эталон _CONSISTENT + _age_lower_of_build), `create_set_plan.py` (комментарий ag011), `DOD.md` (таблица §3.6 + блок возраст-ограничение).
- Статус: 🟡 фикс на Mac, py_compile OK, ждёт деплоя + прогона. Верификация: live socdem tp6-ручной = «25 и старше».
- НЕ помогло ранее: —

### TP67_LIVE_TEXTS_COMPRESSED_AFTER_UAC — live tp6/tp7 терял 3-й текст после UAC-фильтров (2026-07-21)
- Симптом: service-run `ba4754f63984` (`porg-azsw6eyh`, `scherbakova`, `Мультибренд`) блокировал
  tp7-позиции ошибкой `tp6/tp7 live-контент после UAC-фильтров неполный; ... тексты 2/3;
  быстрые ссылки 8/8`.
- Где: `create_set_master_product.py`, финальная UAC/account-pass нормализация перед созданием
  `MasterCampaignSpec`.
- Root-cause: для live-generated tp6/tp7 общий шаблонный fallback намеренно запрещён, но после
  `_coherent_payments`/account-pass/префиксного дедупа тексты могли сжаться с 3 до 2. Добора из
  собственного live-контента после этих фильтров не было, поэтому полный M3-ответ превращался в
  blocked item.
- Решение (2026-07-21): если live-generated tp6/tp7 после финальных UAC-фильтров имеет <3 текстов,
  добираем недостающие тексты только из исходных live-текстов, live-заголовков и описаний быстрых
  ссылок этой же кампании, прогоняя те же guards (`bad_text`, город/бренд, number-gate, длина,
  semantic dedup). Generic fallback по-прежнему не используется.
- Статус: 🟡 локально `py_compile` и `55 passed`; задеплоено на LXC101, worker restarted. Идёт
  повторный service-run: `porg-pl6iavd5` уже `done 18/18 failed=0`; оставшиеся 6 job перезапущены
  через `/direct/api/create_set_async`.
- НЕ помогло ранее: чинить только добор быстрых ссылок — следующий live-прогон вскрыл отдельное
  сжатие текстов до 2/3.

### CREATE_CANCEL_IN_MEMORY_TAIL_CONTINUED — cancel в БД не сразу остановил старый worker (2026-07-21)
- Симптом: после service-cancel ожидавшие jobs стали `cancelled`, но старый `direct-create-worker`
  продолжал выполнять in-memory хвост и при `systemctl restart` ещё грузил картинки для
  `porg-nqavjicg`; `ba4754f63984` успел пройти до `done=18`, `created=3`, `failed=4`.
- Где: `routes_jobs.py::api_job_cancel`, `queue_server.py` in-memory worker loop.
- Root-cause: web-role cancel обновил Victory DB/control, но старый процесс уже держал локальную
  очередь/текущий job и завершал work unit до проверки cancel. Обычный `systemctl restart` ждал
  graceful stop, пока процесс продолжал сетевой этап.
- Решение (2026-07-21): для live-операции процесс остановлен через `systemctl kill -s SIGKILL`,
  затем worker запущен заново; stale write-lock снялся штатным startup sweep. Созданные 3 черновика
  удалены сервисным `/api/jobs/ba4754f63984/delete_created`.
- Статус: 🟡 операционно подтверждено; нужен отдельный код-аудит cancel-loop, чтобы web-role cancel
  быстрее останавливал in-memory worker без SIGKILL.
- НЕ помогло ранее: полагаться только на повторный `/api/create_set_cancel` при уже активном
  long-running сетевом участке.

### OPENROUTER_WHITESPACE_CONTENT_900S — пробельный content отключал first-content cap (2026-07-21)
- Симптом: live create-set (`porg-nqavjicg`, `porg-pl6iavd5`) массово завершался `created=0`,
  `failed=N`, `tone=no_content`; в worker log были `[llm-or] hard-cap 900с — стрим обрезан`,
  хотя `OPENROUTER_FIRST_CONTENT_CAP` должен был переключать на M3 примерно за 90с.
- Где: `llm_providers.py::_consume_sse_stream`.
- Root-cause: first-content cap проверял `not parts`, а OpenRouter мог слать пробельные
  `delta.content`. `parts` становился непустым, cap отключался, но итоговый `content.strip()`
  оставался пустым до 900s hard-cap.
- Решение (2026-07-21): first-content cap теперь ждёт первый `delta.content` с непустым
  `strip()`. Пробельные чанки больше не отключают быстрый fallback.
- Статус: 🟡 нужен повторный service-run после деплоя/restart и очистки черновиков.

### OPENROUTER_NO_CONTENT_STREAM_900S — OpenRouter держал stream без content до hard-cap (2026-07-21)
- Симптом: service-run `96eb5d8ed7cd` (`porg-pl6iavd5`, `scherbakova`, `Мультибренд`,
  `llm_provider=openrouter`) после preupload изображений 15 минут ждал OpenRouter и затем получил
  пачку `[llm-or] hard-cap 900с — стрим обрезан`; 3 item остановлены guard'ом
  `content-fallback-blocked`, создано `0`, failed `18`. Это правильно не создало шаблонные РК,
  но весь набор стал нерабочим.
- Где: `llm_providers.py::_or_complete_url` / `_consume_sse_stream`, live `stream_content` путь
  `create_content.py::run_gen_campaign_content`.
- Root-cause: короткие health-probe M3/OpenRouter были зелёными, но боевой OR stream мог долго
  слать keep-alive/reasoning без `delta.content`. Старый код считал любой SSE-чанк heartbeat'ом
  и ждал общий `OPENROUTER_HARD_CAP=900`, не переводя набор быстро на M3 fallback.
- Решение (2026-07-21): в OR payload явно передаём `reasoning.effort='none'`,
  `reasoning.exclude=true` и `reasoning_effort='none'`; добавлен `OPENROUTER_FIRST_CONTENT_CAP`
  (дефолт 90с). Если за это время нет первого `delta.content`, OR-вызов завершается с диагностикой
  `нет delta.content`, `_is_or_dead()` взводит OR circuit-breaker, остаток набора идёт на M3 без
  повторных 900 секунд.
- Детект-запрос: live-журнал `direct-create-worker.service` с 01:36 показал 6 hard-cap строк и
  `content-fallback-blocked`; после патча standalone OR-call на LXC101 вернул JSON-content за
  несколько секунд при `reasoning=none`.
- Статус: 🟡 локально `55 passed` (`test_tone_voice_generation_contract.py`, `test_routes.py`)
  на Python 3.12, remote `py_compile` OK, worker restarted. Нужен повторный service-run после
  очистки черновиков и проверка tone score/current jobs.
- НЕ помогло ранее: ждать общий `OPENROUTER_HARD_CAP=900` и считать keep-alive/reasoning чанки
  достаточным признаком живой генерации.

### TP7_BUILD_NAME_WRAPPER_DROPPED_TARGETING_LABEL — live-create падал на targeting_label (2026-07-20)
- Симптом: tp7 job создала часть кампаний, но 8 позиций упали с
  `_build_name() got an unexpected keyword argument 'targeting_label'`, например
  `ТК - Общая - аудитории + Автотаргетинг - Краснодарский край`.
- Где: `create_set_master_product.py` fallback-пересборки UAC-имени через DI `_build_name`;
  wrapper `automation_runtime.py::_build_name`.
- Root-cause: `create_set_plan._build_name` уже получил параметр `targeting_label`, но runtime-wrapper,
  который передаётся в `create_set_master_product.configure()`, остался на старой сигнатуре и резал
  новый аргумент. Локальный тест проверял plan helper, но не DI-wrapper live-пути.
- Решение (2026-07-20): `automation_runtime._build_name(..., targeting_label=None)` прокидывает
  label в `create_set_plan._build_name`. Добавлен regression
  `test_runtime_build_name_accepts_tp67_targeting_label`.
- Статус: 🟡 локально подтверждено тестом; live-retry ещё не запускался.
- НЕ помогло ранее: править только `create_set_plan._build_name` без обновления runtime DI-wrapper.

### CREATE_JOB_TIMER_LEAK_ON_QUEUED_CARD — новая queued-карточка наследовала старый таймер (2026-07-20)
- Симптом: новая карточка очереди показывала `⏱ 40:41` сразу после постановки, хотя время должно
  начинаться заново.
- Где: `automation_jobs.js::_jobUpdateCard`.
- Root-cause: live interval/DOM `.job-timer` запускался для `running`, но при переходе карточки в
  `queued`/`awaiting_feed_decision` не очищался и не скрывался. Backend для `queued` elapsed не
  отдаёт, значит старое значение оставалось на клиенте.
- Решение (2026-07-20): при любом статусе кроме `running` очищаем interval, скрываем `.job-timer`
  и удаляем `j.runningFrom`; cache-bust `automation_jobs.js` поднят.
- Статус: 🟡 локально `node --check`; визуальный live-refresh после деплоя нужен на странице.
- НЕ помогло ранее: рассчитывать elapsed только на backend — проблема была в неочищенном frontend
  timer state.

### DELETE_DRAFTS_UAC_400_GRID_FALLBACK — удаление черновиков падало на UAC validation_result (2026-07-20)
- Симптом: кнопка удаления черновиков на `/direct/automation?tab=create` удаляла большую часть
  DRAFT-кампаний, но затем показывала ошибку вида
  `delete 712917162: [delete:712917162] HTTP 400: {"validation_result":...}`.
- Где: `/direct/api/campaigns/delete_drafts_async`, `account_service.py::_delete_drafts_core`.
- Root-cause: после чтения DRAFT-строк из Grid все non-`GdUnifiedCampaign` tool-кампании удалялись
  через UAC DELETE. Если Grid-row была не UAC-типа, уже исчезла/сменила тип или UAC вернул
  `validation_result`, ошибка считалась terminal и попадала в UI, хотя безопасный Grid-delete по
  этому id мог удалить или подтвердить отсутствие черновика.
- Решение (2026-07-20): UAC-ветка теперь при ошибке пробует одноточечный
  `GridCreateClient.delete_campaigns([id])`; если он сработал или повторное чтение DRAFT-списка
  показывает, что id уже отсутствует, позиция считается успешно удалённой через cookie/Grid.
  Ошибка возвращается пользователю только если черновик всё ещё виден или Grid-проверка недоступна.
- Детект-запрос: локальный regression-test
  `test_delete_drafts_uac_400_falls_back_to_grid_delete` воспроизводит UAC HTTP 400 на
  `GdCampaign` и проверяет `deleted=1`, `failed=0`, пустой `errors`.
- Статус: 🟡 локально подтверждено тестами; live-delete повторно не запускался, чтобы не удалять
  новые черновики без явного действия пользователя.
- НЕ помогло ранее: считать любой UAC HTTP 400 окончательной ошибкой — из-за этого UI показывал
  красную ошибку после частично успешной очистки.

### TP7_TARGETING_LABEL_WITHOUT_AUTOTARGET — товарка показывала ручной таргетинг отдельно (2026-07-20)
- Симптом: в `tp7 ТК` на «Структуре слепков»/«Создании РК» появлялись отдельные бейджи
  `КС` или `аудитории`, хотя для товарной кампании эти режимы не существуют отдельно от
  автотаргетинга.
- Где: `create_set_context.py::tp67_targeting_label_from_modes`, `slepki_ui.js::_tp67Targeting`.
- Root-cause: предыдущий фикс рассинхрона `item.t` заменил источник на фактический контент, но helper
  остался общим для tp6/tp7. Для tp7 не был зафиксирован доменный invariant: ручные ключи/аудитории
  всегда отображаются как добавка к автотаргетингу.
- Решение (2026-07-20): `tp67_targeting_label_from_modes(modes, tp)` теперь принимает tp-контекст;
  для `tp7` возвращает `КС + Автотаргетинг`, `аудитории + Автотаргетинг` или
  `КС + аудитории + Автотаргетинг`. `create_set_plan.py`, fallback в
  `create_set_master_product.py` и frontend-бейджи используют ту же семантику.
- Детект-запрос: regression tests в `test_tp67_targeting_label_uses_content_not_stale_item_text` и
  `test_set_plan_tp7_name_keeps_autotargeting_in_manual_labels`.
- Статус: 🟡 локально подтверждено тестами/синтаксисом; live-create не запускался.
- НЕ помогло ранее: считать tp6 и tp7 одинаковыми после перехода с `item.t` на фактический контент.

### CAMP_NAMES_CROSS_TP_LEAK — чужие camp_names попадали не в тот tp (2026-07-20)
- Симптом: в `scherbakova/tp1 РСЯ` отображались кампании `Поиск - Марки - Автотаргетинг`,
  `Поиск - Модели - Автотаргетинг`, `Поиск + Динамика - Модели - Автотаргетинг`; такие же значения
  могли уйти в план создания через `structure_to_campaigns`.
- Где: `automation.js::_campaignize`, `create_set_structure.py::structure_to_campaigns`.
- Root-cause: `item.camp_names` хранит живые имена из кабинета и у части слепков содержит строки
  соседних типов кампаний. UI и план доверяли списку целиком, без проверки семейства текущего tp.
- Решение (2026-07-20): добавлена совместимость `camp_name ↔ tp`: `tp1=РСЯ`, `tp2=Поиск -`,
  `tp3=Товарная галерея/ТГ`, `tp4=Поиск + Динамика`, `tp5=Поиск + Динамика + ТГ/Товарная галерея`.
  Backend и frontend фильтруют `camp_names`; если после фильтра список пустой, применяется старый
  fallback по split/сегменту, чтобы группа не терялась.
- Детект-запрос: `test_structure_to_campaigns_filters_cross_tp_camp_names` проверяет реальные
  загрязнённые слепки `scherbakova/tp1` и `karavaev/tp2`; полный аудит через
  `structure_to_campaigns` после фильтра оставляет только допустимые имена, кроме non-auto `dmp/tp2`
  split-labels.
- Статус: 🟡 локально подтверждено тестами/аудитом; live-create не запускался.
- НЕ помогло ранее: группировка `camp_names` 1:1 без проверки префикса кампании.

### CONTENT_RENAME_GRID_NARROW_INTERNAL_ERROR — campaign rename падал на неполном Grid payload (2026-07-20)
- Симптом: `GridClient.set_campaign_names` после 3 ретраев за ~95с стабильно получал
  `Внутренняя ошибка сервера, reqId=...` на одной и той же мутации/кампании.
- Где: `/direct/automation/content`, вкладка «Смена названий»; `grid_finalize.py::set_campaign_names`.
- Root-cause: HAR `direct.yandex.ru.68har.har`/`69har.har` показал, что веб-морда Директа при
  rename не отправляет узкий `{id,name}`/`_narrow_campaign_base` payload. Она читает
  `CampaignsEditData` и отправляет полный `unifiedCampaign` в `UpdateCampaigns`, включая
  `biddingStategyWithPlatforms` (именно с опечаткой `Stategy`), `strategyId`, platforms,
  minus, bidModifiers и другие поля. Наш narrow writer терял эти поля и мог ронять Grid resolver.
- Решение (2026-07-20): `set_campaign_names` переведён на full-object RMW:
  `_read_unified_campaign_update_payloads(ids)` → `_narrow_bases` → заменить только `name` →
  `post_idempotent("UpdateCampaigns", ...)`. Ретрай оставлен для настоящих транзиентных 5xx.
  Для `set_adgroup_names` добавлен HAR-parity по ретаргетингам: `GroupsForEditLite` читает
  `retargetingConditionId`/`retargetingId`, `build_update_item` отправляет
  `retargetings: [{retCondId, id}]` и `retargetingCondition: null`, поэтому группы с
  ретаргетингами больше не пропускаются при rename.
- Детект-запрос: локальный regression-test
  `test_grid_set_campaign_names_uses_full_campaign_payload` проверяет, что strategy/minus поля
  сохраняются в отправляемом rename payload; `test_grid_set_adgroup_names_preserves_retargetings`
  проверяет сохранение group retargetings.
- Статус: ✅ задеплоено на LXC101; Mac↔LXC md5 совпал, remote `py_compile` OK,
  `direct-content.service` и `direct-content-worker.service` active. Живой rename на
  `porg-gcegsszl` ещё не запускался в этой сессии.
- НЕ помогло ранее: только retry 3 попытки — полезен для транзиентных 500, но не чинит
  детерминированный internal error от неполного payload.

### CREATE_SET_FEED_AWAIT_RACE — web-worker мог забрать job до решения по фиду (2026-07-20)
- Симптом: при `feed_alert.needed=true` web endpoint отвечал `awaiting_feed_decision`, но job сначала
  вставлялась как `queued`; отдельный worker мог успеть заклеймить её и начать создание без выбора
  пользователя. UI при реальном `awaiting_feed_decision` показывал карточку как terminal/ошибочную.
- Где: `/direct/api/create_set_async`, `/direct/api/create_set_feed_decision`,
  `routes_jobs.py`, `queue_server.py`, `job_repository.py`, `automation_jobs.js`.
- Root-cause: ожидание фида применялось отдельным post-insert `UPDATE queued→awaiting_feed_decision`,
  а worker клеймит именно `status='queued'`. Дополнительно frontend делал pre-job запрос
  `create_set_feed_decision` без `job_id`, который backend всегда отклонял.
- Решение (2026-07-20): web-route перед `job_new` кладёт `_feed_deadline` и внутренний
  `_initial_status='awaiting_feed_decision'`; `_job_new_web` вставляет строку сразу в этом статусе,
  недоступном для worker-claim. UI job-stack получил ветку `awaiting_feed_decision` с кнопками,
  отправляющими `job_id`; pre-job вызовы без `job_id` удалены. `_job_db_web_await_feed` теперь
  возвращает `False` при нулевом update/ошибке, если legacy-путь снова понадобится.
- Детект-запрос: локальные regression tests
  `test_create_set_async_web_feed_alert_posts_initial_awaiting_status`,
  `test_create_set_async_rejects_empty_login_before_queueing`,
  `test_delete_created_web_role_reads_job_from_database`; grep должен показывать
  `create_set_feed_decision` только в `automation_jobs.js` с `job_id`.
- Статус: 🟡 локально подтверждено тестами и синтаксическими проверками; живой product-run не
  запускался, чтобы не ставить реальную задачу создания в очередь.
- НЕ помогло ранее: post-insert `job_db_web_await_feed(job_id, deadline)` — оставлял окно гонки и
  молчал при `rowcount=0`.

### TP67_TARGETING_LABEL_DRIFT — имя tp6/tp7 брало таргетинг из устаревшего item.t (2026-07-20)
- Симптом: tp6/tp7 имя обещало один таргетинг, а payload получал другой: `Автотаргетинг` при
  100+ аудиториях, `КС` при ключах+аудиториях, либо `КС` при пустом корпусе.
- Где: `/direct/api/set_plan`, вкладка «Создание РК», страница «Структура слепков»;
  `create_set_plan.py`, `create_set_context.py`, `slepki_ui.js`, `automation_create.js`.
- Root-cause: targeting-хвост имени и UI-бейдж читали статичный harvest-текст `item.t`. Реальный
  режим уже считался отдельно из содержимого позиции (`keywords` в pack_facts + аудитории
  структуры), но в человекочитаемое имя не попадал.
- Решение (2026-07-20): добавлен единый helper `tp67_targeting_label_from_modes()`:
  `КС` / `аудитории` / `КС + аудитории` / `Автотаргетинг`. План tp6/tp7 очищает старый
  targeting-хвост из `item.t` и добавляет фактическую метку из `tp67_struct_expectations`.
  UI tp6/tp7-бейдж сначала смотрит на server-side `pack_facts` и поддержанные аудитории позиции,
  а `item.t` использует только как fallback.
- Детект-запрос: локальный regression-test
  `test_set_plan_tp67_name_appends_computed_targeting_label` проверяет, что позиция с
  устаревшим `item.t="... - КС"` и фактом `keywords+audience` получает имя с
  `КС + аудитории`, без старого хвоста.
- Статус: 🟡 локально фиксируется тестами/синтаксисом; live-create не запускался, чтобы не ставить
  реальную задачу создания в очередь.
- НЕ помогло ранее: регулярки по `item.t` и `gk` (`*_интересы`) — это аннотации харвеста, а не
  гарантия фактического контента позиции.

### TONE_VOICE_ALL_SLEPKI_LOW_SCORE — checker смешивал кампании аккаунта + generic fallback у всех слепков (2026-07-20)
- Симптом: на `/direct/automation?tab=create` тон-войс ругался не только на
  `Слепок_Гордеева/Монобренд`, а на разные слепки в одном логине (`porg-ozge4ntu`):
  scores 0–30/100, generic-примеры вроде `Кредит от 15 банков`, `Купить новое авто в кредит`,
  `Рассрочка`.
- Где: `tools/check_tone_of_voice.py`, `ai_agents.py`, `create_content.py`,
  `create_set_assets.py`, `text_gen.py`, `automation_runtime.py`, `create_set_feed_builders.py`,
  `create_set_master_product.py`.
- Root-cause #1: `check_tone_of_voice.read_content_v5()` при пустом `campaign_ids` делал fallback
  на `_all_campaigns(login)` для обычного job. Parent/requeue/resume jobs часто не содержат ids
  напрямую, поэтому checker сравнивал текущий слепок с остатками всего аккаунта.
- Root-cause #2: общие deterministic fallback-пулы могли добивать будущие РК одинаковыми
  фразами `Кредит от 15 банков`, `Новые авто в кредит`, `Купить новое авто в кредит`,
  `Рассрочка без переплат`, что реально снижало score даже без смешивания.
- Решение (2026-07-20): checker рекурсивно извлекает `campaign_id` из result и добирает ids из
  child jobs (`_resume_of`/`_requeue_of`); fallback на весь аккаунт оставлен только для `adhoc:*`.
  При отсутствии ids обычный job возвращает `no content` с note и не создаёт ложный low-score.
  В генерации добавлен общий стоп-фильтр generic-credit/installment; любой маркер `15 банков`
  для объявлений теперь режется независимо от соседнего слова. Runtime fallback строки заменены
  на `Кредитное решение`/`кредит по заявке` без `15 банков`/`рассрочки`; corpus/promo examples
  дополнительно фильтруются до попадания в prompt.
- Детект-запрос (LXC101):
  ```bash
  cd /opt/scripts/home/seoadvanced
  DIRECT_ROLE=web PYTHONPATH=/opt/scripts/home/seoadvanced:/opt/scripts/.secret \
    /root/venv/bin/python3 -m direct.tools.check_tone_of_voice 30f980bbb63b --dry --test
  DIRECT_ROLE=web PYTHONPATH=/opt/scripts/home/seoadvanced:/opt/scripts/.secret \
    /root/venv/bin/python3 -m direct.tools.check_tone_of_voice 37fd8ad1c62f --dry --test
  ```
  Норма: no-id job даёт `no content` и note `не проверяю весь аккаунт`; parent job проверяет
  только свои child campaign_ids.
- Статус: ✅ задеплоено на LXC101; Mac↔LXC sha256 совпали по 8 Python-файлам, remote `py_compile` OK,
  `direct-create.service`, `direct-create-worker.service`, `tone-of-voice-watcher.service`
  перезапущены и active; отдельный `direct_verifier` подтвердил отсутствие exact generic-маркеров
  в исполняемых fallback-строках локально и на LXC101. Старые live-кампании с уже созданными
  generic-объявлениями по-прежнему могут получать mixed/low до отдельного repair/пересоздания.
- НЕ помогло ранее: точечный override одного слепка (`gordeeva/Монобренд`) — дефект общий,
  затрагивал все слепки и parent jobs.

### TONE_VOICE_TEMPLATE_FALLBACK_IN_LIVE_CREATE — live-генерация добивала LLM шаблонами (2026-07-21)
- Симптом: даже после чистки obvious generic-маркеров tone-score новых/недавних create jobs мог
  оставаться ниже 60: тексты выглядели одинаковыми, потому что часть финального комплекта бралась
  из корпуса слепка, `direct_slepok_content` или статических fillers, а не из нового LLM-ответа.
- Где: `ai_agents.py::assemble_campaign`, `create_content.py::run_gen_campaign_content`,
  `create_set_orchestrator.py` stream_content path.
- Root-cause: шаблоны задумывались как few-shot примеры, но боевой путь использовал их ещё и как
  финальный fallback: `assemble_campaign()` добирал недостающие titles/texts/sitelinks из
  `agent["ads"]`, `_final_fill_campaign_content()` добавлял статические fillers, а при полном
  провале LLM `run_gen_campaign_content()` мог взять `direct_slepok_content`. В итоге модель могла
  сгенерировать 0–2 валидных строк, а кампания всё равно создавалась с template-like контентом.
  Дополнительный обход найден 2026-07-21: `create_set_master_product.py` для tp6/tp7 ct0000
  получал полный `it["titles"]/texts/sitelinks` от stream_content, но затем игнорировал
  `it["titles"]`/`it["texts"]` и заново собирал UAC-креативы из `_GENERIC_*`/`tpl_*`.
- Решение (2026-07-21): у `assemble_campaign()` добавлен режим `allow_corpus_fill=False`; live
  `run_gen_campaign_content()` вызывает его только так. `_final_fill_campaign_content()` в боевом
  пути работает с `allow_static_fill=False`; fallback на `direct_slepok_content` для live removed.
  Если после LLM/repair полного комплекта нет, функция возвращает `ok:false` с ошибкой
  `шаблонный фолбэк запрещён`, а `create_set_orchestrator.py` записывает item error и не создаёт
  generic-черновик. `create_set_master_product.py` теперь в live-stream режиме берёт tp6/tp7
  заголовки/тексты/сайтлинки только из LLM item-контента; `_GENERIC_*`, `tpl_*`, корпус слепка и
  `_fallback_master_titles` не добивают UAC-креатив. Если UAC-фильтры выкинули часть LLM-комплекта,
  item блокируется той же ошибкой `шаблонный фолбэк запрещён`. In-memory account-content cache после
  рестарта очищается, persistent cache в БД нет.
- Детект-запрос: regression
  `direct/tests/test_tone_voice_generation_contract.py::test_assemble_campaign_live_mode_does_not_copy_agent_corpus`
  и `::test_live_generation_blocks_template_fallback_when_llm_is_empty`;
  `::test_tp67_live_master_product_does_not_fill_from_templates_after_llm` фиксирует tp6/tp7 обход.
- Статус: 🟡 локально подтверждено (`68 passed` вместе с route/source/architecture тестами);
  live-create после фикса ещё не запускался, поэтому старые строки `direct_tone_checks` ниже 60
  не являются доказательством текущего нового пути. Норма для следующего прогона: либо новый
  LLM-контент получает tone-score ≥60, либо item падает явной ошибкой вместо создания generic РК.
- НЕ помогло ранее: только удалять фразы `15 банков`/`рассрочка` — это убирало маркеры, но не
  запрещало сам механизм копирования шаблонов в финальный контент.

### COPY_API_RAW_PAYLOAD_CONTRACT — внешний API пропускал сырые feed/image/geo поля (2026-07-20)
- Симптом: для интеграции `/api/v1/copy/start` тело job могло отличаться от нормализованного UI-пути:
  `feed_map` оставался raw до движка, `image_hashes` мог содержать нестроковые элементы до поздней
  проверки, `geo_region_ids` при `geo_mode=keep` попадал в job без смысла, а при `geo_mode=change`
  mixed-список частично фильтровался вместо 400.
- Где: `copy_api.py:register_copy_api`, `copy_request.py:validate_other_geo`.
- Root-cause: public API делал allowlist тела клиента, но не все поля нормализовал на границе API
  перед расчётом idempotency hash и постановкой job. `validate_other_geo` был tolerant для UI и API.
- Решение (2026-07-20): `copy_api.py` нормализует `feed_map`/`image_hashes` до job; raw
  `geo_region_ids` убран из allowlist и добавляется только валидированный список при
  `mode=other, geo_mode=change`; `copy_request.validate_other_geo(surface="api")` возвращает
  `400 INVALID_GEO` на mixed/non-integer элементы.
- Детект-запрос: локальный API-smoke в тестах:
  `test_public_copy_api_normalizes_feed_map_before_queue`,
  `test_public_copy_api_drops_geo_region_ids_when_geo_mode_keep`,
  `test_public_copy_api_rejects_mixed_geo_region_ids_for_change`.
- Статус: ✅ подтверждено тестами 2026-07-20 (`15 passed`) и read-only verifier.
- НЕ помогло ранее: нормализация только внутри `copy_engine` — поздно для внешнего API-контракта
  и idempotency.

### COPY_CLEANUP_BEFORE_VALIDATION — cleanup цели мог выполниться до preflight/feed validation (2026-07-20)
- Симптом: при `target_cleanup=delete_drafts|archive` внешний/API/UI запуск мог сначала удалить или
  архивировать кампании цели, а потом упасть на неполном source snapshot, неизвестном geo или битой
  карте фидов.
- Где: `copy_engine.py:_copy_run_job`.
- Root-cause: cleanup стоял в начале job до pull/preflight/geo/feed validation.
- Решение (2026-07-20): cleanup перенесён после source pull, `_copy_snapshot_preflight`,
  geo rewrite validation и `_copy_validated_feed_map`, но до upload; grid-cookie unified-only ветка
  запускает cleanup перед Grid copy, после определения выбранных unified campaigns.
- Детект-запрос: code review path + тестовый запуск маршрута без live-write; live-деструктивный
  сценарий намеренно не воспроизводился.
- Статус: 🟡 задеплоено, ждёт следующего copy-run с cleanup.
- НЕ помогло ранее: подтверждение cleanup только через UI confirm — не защищало от падений после старта.

### COPY_WEEKLY_CLICKS_HAR_ENUM — weekly OPTIMIZE_CLICKS пропускался как unsupported (2026-07-20)
- Симптом: 7 draft РК на `porg-lzjk6p5m` (`712903434`, `712903438`, `712903455`, `712903461`,
  `712903464`, `712903471`, `712903472`) пропускались в `set_campaign_invariants` как
  `Максимум кликов (недельный бюджет)`, поэтому оставались расхождения по Grid-only настройкам.
- Где: `grid_finalize.py:_strategy_update_payload`, `_unified_campaign_update_from_edit_row`.
- Root-cause: старый guard считал, что у weekly `OPTIMIZE_CLICKS` нет безопасного write-enum.
  HAR `direct.yandex.ru.67har.har` показал реальную UI-форму `UpdateCampaigns`:
  `strategyName=AUTOBUDGET_AVG_CLICK`, `strategyData.avgBid`, `sum`, `budgetType=WEEKLY`.
- Решение (2026-07-20): weekly `OPTIMIZE_CLICKS` с бюджетом пишется как `AUTOBUDGET_AVG_CLICK`;
  если Grid read отдаёт `avgBid=None`, используется UI-дефолт `100`. `_unsupported_strategy`
  оставлен только для `OPTIMIZE_CLICKS` без `clicksLimit`, `avgBid` и бюджета.
- Детект-запрос: read-only live probe `_read_unified_campaign_update_payloads` по 7 cid показал
  `unsupported=-`, `strategyName=AUTOBUDGET_AVG_CLICK`, `avgBid=100`, `sum=300`, `budgetType=WEEKLY`.
- Статус: ✅ код/контракт подтверждён тестами и read-only live-probe; live `UpdateCampaigns`
  на эти 7 РК не запускался.
- НЕ помогло ранее: отправка `AUTOBUDGET` — меняла стратегию на максимум конверсий, поэтому была запрещена.

### COPY_VERIFY_SOURCE_SHAPE_DRIFT — verifier падал/завышал mismatch из-за формы Direct/Grid (2026-07-20)
- Симптом: `copy_verify_settled` для job `4c0c992cf213` сначала падал
  `unsupported operand type(s) for +: 'dict' and 'list'`, затем завышал mismatch по TextAd,
  когда Grid возвращал пустую adaptive-row без payload.
- Где: `copy_verify_source.py:build_source_profile`.
- Root-cause: Direct `ExcludedSites`/`ExcludedSitesForVideoAds` может прийти dict-shape `{"Items":[]}`,
  а не list; пустая Grid-row от `adaptive_ads_for_update` маскировала v5 `TextAd` fallback.
- Решение (2026-07-20): `_items_list()` нормализует dict/list shapes; `_grid_ad_has_payload()`
  включает Grid override только при наличии реального payload. Покрыто тестами.
- Детект-запрос: повторный `copy_verify_settled` для `4c0c992cf213` больше не падает:
  summary `ok=374, missing=1, mismatch=124, unreadable=29`.
- Статус: ✅ задеплоено, remote `py_compile` OK, verifier принят.
- НЕ помогло ранее: считать любую Grid-row авторитетной даже без payload.

### COPY_CAMPAIGNS_ADD_1000_TRANSIENT — campaigns.add падал с кодом 1000 и не ретраился (2026-07-20)
- Симптом (job `2b0e66bf18ae`): 3 кампании FAIL с `[campaigns.add] 1000: Сервис временно недоступен` — транзиент, но без ретрая окно валило РК насовсем.
- Root-cause: в `phase_upload` (direct_copy.py) после fallback-вызова без Settings при `__error__` сразу писался FAIL. Ретрая на транзиент не было.
- Решение: если после fallback в `__error__` есть маркер `"1000"` или `"временно недоступен"` — повторять `api_mutate("campaigns","add",...)` до 3 раз с backoff `time.sleep(2*(try+1))`; ретрай безопасен т.к. сервер вернул явный отказ (запрос не применился). На детерминированной ошибке (без маркера) — сразу FAIL без ретрая.
- Где: `work/slepki_direktologov/scripts/direct_copy.py` строки 1274–1289 (после второго `if "__error__" in res:`).
- Статус: 🟡 ждёт живого прогона.

### COPY_ADS_ADD_9300_OVER_1000 — ads.add падал на 9300 (>1000 объявлений в запросе) (2026-07-20)
- Симптом (job `2b0e66bf18ae`): ~100× `[ads.add] 9300: Разрешено создавать не более 1000 объявлений в одном запросе` — ListingAd разворачивался в много bodies → набегало >1000.
- Root-cause: `phase_upload` отправлял все bodies одного ad-group одним запросом без ограничения размера.
- Решение: разбить `group` на чанки по `_ADS_ADD_CHUNK = 1000`, каждый чанк — отдельный `api_mutate`; zip результатов делается по `chunk`, не по всему `group` (иначе разъезжается).
- Где: `work/slepki_direktologov/scripts/direct_copy.py` строки 1528–1550 (секция ads.add).
- Статус: 🟡 ждёт живого прогона.

### COPY_REVERIFY_NAMEERROR_CSTEP_CTX — NameError cstep_ctx в планировщике ре-верификации (2026-07-20)
- Симптом (job `2b0e66bf18ae`): лог `copy_verify reverify schedule error: name 'cstep_ctx' is not defined` — отложенная пере-сверка не стартовала.
- Root-cause: `copy_engine._copy_run_job` строка 740 ссылалась на `cstep_ctx.geo_pairs`, но `cstep_ctx` не определён в scope `_copy_run_job` (разные модули/контексты после распила). `rewrite_meta` с ключом `pairs` уже был в scope и содержит корректные гео-пары.
- Решение: заменить `cstep_ctx.geo_pairs or []` на `rewrite_meta.get("pairs") or []`.
- Где: `home/seoadvanced/direct/copy_engine.py` строка 740.
- Статус: 🟡 ждёт живого прогона.

### FOREIGN_SCRIPT_INTERRUPTS_LIVE_JOB — чужой одноразовый скрипт помечает ЖИВУЮ джобу interrupted (2026-07-20)
- Симптом: джоба `kryuchkova`/Мультибренд (`404c320fc32e`, `porg-ozge4ntu`) реально создавала
  кампании (3/25, прогресс есть, `_heartbeat` тикал по каждому LLM-токену), но статус в БД внезапно
  стал `interrupted` — QA-драйвер зафиксировал `FAIL status=interrupted done=3/25`. `direct-create-worker.service`
  НЕ перезапускался (`journalctl` — ни одного restart-события в окне), тот же PID/тот же процесс живёт
  без перерыва. py-spy live dump подтвердил: воркер-тред стоял на реальном сетевом чтении SSE-потока
  генерации текста (`_consume_sse_stream`, `llm_providers.py:226`), не на deadlock/сбое.
- **Root-cause:** `queue_server._jobs_db_recover()` — единственное место в коде, где статус становится
  `interrupted` — глобально помечает ЛЮБУЮ `status='running'` строку в общей таблице
  `direct_automation_jobs` (кроме `kind='copy_campaigns'`/edit-джоб слепков), не разбирая, чей это
  воркер. Она запускается из `_ensure_create_worker_started()` при ПЕРВОМ вызове в процессе, если роль
  ≠ `web`. Старый гейт (`if not DIRECT_ROLE and not INVOCATION_ID: return`, чинил инцидент 2026-07-06)
  пропускал ЛЮБОЙ одноразовый скрипт, который явно выставил `DIRECT_ROLE` (`worker`/`all`) — не обязан
  быть настоящим systemd-сервисом. На хосте параллельно крутятся десятки диагностических python-скриптов
  (в т.ч. read-only «живые проверки» через `app.test_client()`/`from direct.main import app`) — если
  такой скрипт явно ставит `DIRECT_ROLE` (даже случайно, copy-paste из окружения соседнего сервиса) и
  НЕ ставит `DIRECT_ROLE=web`, при первом импорте `direct.main` в СВОЁМ процессе он глобально стирает
  статус ВСЕХ реально работающих джоб создания на общем аккаунте/сервере.
- Где: `queue_server.py:1968` (`_ensure_create_worker`), гейт перед вызовом `_jobs_db_recover()`.
- Решение (2026-07-20): гейт сужен до **только** `INVOCATION_ID` (systemd проставляет его
  ТОЛЬКО управляемым юнитам, вручную/скриптом подделать нельзя) — `DIRECT_ROLE` из условия убран
  полностью. Проверено: все 4 реальных сервиса (`direct-create-worker`, `direct-slepki-worker`,
  `direct-copy`, `direct-create`) имеют `INVOCATION_ID` в `/proc/<pid>/environ` — фикс их не задевает.
  `slepok_qa_run.py` ставит `DIRECT_ROLE=web` ДО импорта — отсекается более ранним гейтом
  (`if _direct_role() == "web": return`), эта правка его не касается.
- Детект-запрос (read-only, на LXC101): PID реальных сервисов и наличие `INVOCATION_ID`:
  ```bash
  for pid in $(systemctl show direct-create-worker.service direct-slepki-worker.service \
               direct-copy.service direct-create.service -p MainPID --value); do
    grep -q INVOCATION_ID "/proc/$pid/environ" 2>/dev/null && echo "$pid OK" || echo "$pid MISSING"
  done
  ```
  Ожидание: все `OK`. Если после деплоя правки где-то `MISSING` — юнит не под systemd, разбираться отдельно.
- Статус: 🟡 фикс закоммичен, живым повторным инцидентом ещё не проверено (совпадение по времени с
  чужим скриптом не гарантировано воспроизводимо намеренно) — если `interrupted` на реально прогрессирующей
  джобе повторится ПОСЛЕ деплоя этой правки, это будет означать другой источник и нужен новый разбор.
- НЕ помогло ранее: гейт «DIRECT_ROLE ИЛИ INVOCATION_ID» (инцидент 2026-07-06) — работал против
  скриптов БЕЗ роли вообще, но не против скриптов, ЯВНО эту роль выставляющих.
- ⚠️ Побочный урок: все read-only «живые проверки» этой сессии, которые открывали `direct.main`/
  `app.test_client()` НАПРЯМУЮ без `DIRECT_ROLE=web`, теоретически МОГЛИ триггерить эту же дыру раньше —
  задним числом непроверяемо (нет лога, кто именно). Начиная с этой правки риск закрыт для всех, у кого
  нет `INVOCATION_ID`, независимо от того, что они ставят в `DIRECT_ROLE`.
>
### COPY_DEDUP_KEYWORDS — MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS в UpdateUnifiedAdGroups (2026-07-20)
- Симптом: 66× ошибка `CollectionDefectIds.Gen.MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS, path: updateAdGroupItems[0].keywords[4]` при копировании 173 кампаний; Директ отклонял ВЕСЬ апдейт группы целиком.
- Root-cause: `build_update_item` в `grid_finalize.py` не дедуплицировал коллекции перед отправкой. Если у исходной unified-группы фраза повторялась — копия шла с дублями в `keywords`/`adGroupMinusKeywords`/`libraryMinusKeywordsIds`/`regionIds`.
- Где: `grid_finalize.py:3290–3298` (`build_update_item`).
- Решение (2026-07-20): дедуп с сохранением порядка через `dict.fromkeys` на всех четырёх коллекциях; срезы `[:200]`/`[:100]` и фолбэк `or [225]` сохранены.
- Статус: 🟡 фикс готов (py_compile OK), ждёт деплоя и живого прогона копирования.
- НЕ помогло ранее: —

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
- Детект-запрос: готовый SQL/API-вызов, который считает ЧИСЛО дефектов этой сигнатуры в live
  (обязателен — им `direct_verifier` строит таблицу «дефект / было / стало»; без него фикс
  нельзя подтвердить фактом)
- Статус: ✅ подтверждено прогоном <дата> | 🟡 ждёт прогона | ❌ не помогло (что дальше)
- НЕ помогло ранее: (если были неудачные попытки — обязательно, чтобы не повторять)
```

---

## Активные / недавние ошибки

### UNIQ_EXISTING_COLLISION_MINTS_DUP — повторный `/set_plan` на населённом кабинете плодит `base_v01` поверх живой `base` (2026-07-20)
- Симптом: ручной повторный «Создать набор» (`/set_plan`) на УЖЕ населённом кабинете (сценарий
  восстановления — доставка недостающих позиций БЕЗ предварительного сноса черновиков) создаёт дубли
  с суффиксом `_v01` поверх реально существующих кампаний. Живой факт: аккаунт `porg-ozge4ntu`
  (kryuchkova/Мультибренд) — 11 дублей `base_v01` при живых `base` (уже удалены вручную).
- Где: `create_set_plan.py:_uniq` (было :546-555) — единая воронка плановых имён tp1–tp7.
- Root-cause: при конфликте имени `_uniq` НЕ различал два случая — (а) две РАЗНЫЕ позиции ОДНОГО
  плана на одно имя (легитимно `_v01`) и (б) имя совпало с УЖЕ ЖИВОЙ кампанией кабинета (`existing`,
  собран `:420-428`) = RESUME-SKIP, а не новая позиция. Оба трактовались как (а) → минтился `base_v01`.
  Дальше `already_in_direct` (`create_set_resume.py:19-35`) нормализует `_vNN` только на LIVE-стороне,
  а TARGET-имя уже несёт `_v01` → `already_in_direct("base_v01",{"base"})`=False → «новая» `base_v01` →
  дубль. 4 фидовых tp1 «повезло» (live-имя `base — feedlabel`, суффикс клеится на билде → `_uniq(base)`
  не находит `base` в existing → план строит `base` чисто → skip); 11 нефидовых (live-имя ровно `base`)
  «не повезло» → коллизия с existing → `_v01`. НЕ задевает reconciler `_requeue_missing_positions_once`
  (работает с готовыми `items`, `_uniq`/replan не зовёт) — только ручной повторный `/set_plan`.
- Решение (2026-07-20): в `_uniq` развели коллизии. Имя в `existing`, но НЕ в `used` (позиции текущего
  плана) → RESUME-SKIP: вернуть имя НЕТРОНУТЫМ (`return name, False`), не минтить — `already_in_direct`
  на чистом `base` даст точный матч и штатно пропустит. Коллизия с `used` → как раньше, `_v01`/`_v02`
  (в т.ч. поверх занятого именем в кабинете). Правка целиком в `create_set_plan.py`,
  `queue_server.py`/`create_set_resume.py` НЕ тронуты.
- Детект-запрос (python, на LXC101 — считает дубли `X_vNN` при живом `X` в кабинете):
  ```python
  from create_set_resume import _VNN_RE
  names = {(c.get("name") or "").strip() for c in _grid_list_campaigns("porg-ozge4ntu")}
  dupes = [n for n in names if _VNN_RE.search(n) and _VNN_RE.sub("", n).strip() in names]
  print(len(dupes), dupes)
  ```
  Baseline на момент фикса: 0 (11 дублей инцидента удалены вручную ДО правки — число подтверждает
  чистоту кабинета, не сам фикс). После деплоя повторный `/set_plan` на населённом кабинете НЕ должен
  поднимать это число выше 0.
- Статус: 🟡 фикс закоммичен, деплой/рестарт за главной сессией, живым повторным `/set_plan` ещё не проверено.
- НЕ помогло ранее: нет — первая правка этой сигнатуры.

### REQUEUE_STRIPS_FEED_CONFIRMED_TP5_GATE_FAILS — reconciler-requeue теряет подтверждение фида → все tp5/tp3 падают (2026-07-20)
- Симптом: requeue-ребёнок (доставка недостающих позиций) на аккаунте без профильного фида
  (`/yandex.xml`|`/yandex-used-auto.xml`) молча роняет ВСЕ tp5: `single_feed: целевой фид не найден
  — tp5 пропущена`. Живой факт (инцидент `404c320fc32e`, `porg-ozge4ntu`, kryuchkova/Мультибренд):
  child `a712bbb0e104` пришёл с `feed_confirmed=None, single_feed_fallback=None`, 6 tp5 упали одинаково.
- Где: `queue_server.py:_requeue_missing_positions_once` — построение тела доставки `rbody`
  (было :582-584) стрипало `feed_confirmed` вместе с транзиентным `feed_alert`. tp5/tp3-гейт билда
  `create_set_feed_builders.py:_resolve_single_feed_variants:926` открывает каталог-фолбэк только при
  `single_feed_fallback OR feed_confirmed` → без обоих `data["feeds"]=[]` → отказ (`:960-964`).
- Root-cause: `feed_confirmed` — не только UI-флаг awaiting_feed_decision, он же несёт durable
  ПОДТВЕРЖДЕНИЕ фолбэк-фида (кнопка «Продолжить с другим фидом»), которое читают plan-гейт
  (`create_set_plan.py:474`) и build-гейт. tp7 «повезло» — резолвит фид на этапе ПЛАНА оригинальной
  (непрерванной) джобы, где флаг цел; tp5/tp3 резолвят на БИЛДЕ requeue-ребёнка, где флаг уже вырезан.
  **Системная дыра — не специфична для kryuchkova**: сработает у ЛЮБОГО аккаунта без профильного фида
  при requeue tp5/tp3.
- Решение (2026-07-20): в `queue_server.py` после сборки `rbody` транслируем `feed_confirmed →
  single_feed_fallback` (durable plan-ключ, НЕ стрипается, принимается обоими гейтами);
  `feed_alert`/`feed_confirmed` остаются вырезаны → ребёнок не входит в awaiting_feed_decision заново.
- Детект-запрос (python, на теле requeue-ребёнка): после фикса `build_rbody(body)` для
  `body={"feed_confirmed":True,"single_feed":True}` должен дать `single_feed_fallback=True`, а
  `_resolve_single_feed_variants`-гейт (`_fb_body.get("single_feed_fallback") or feed_confirmed`) —
  True. Живой детект в кабинете: у аккаунта план с tp5 и БЕЗ `/yandex.xml`/`/yandex-used-auto.xml`,
  прогнанный через requeue — число созданных tp5-кампаний (`tp5_cpc_*`/`tp5_cpa_*`) должно быть > 0
  (было 0). Симуляция логики: `scratchpad/sim.py` (crit.1) — было tp5_gate=False, стало True.
- Статус: 🟡 фикс закоммичен, деплой/рестарт за главной сессией, живым повторным прогоном ещё не проверено.
- НЕ помогло ранее: нет — первая правка этой сигнатуры.

### RECONCILER_ONE_SHOT_MARKER_LOSES_POSITIONS_SILENTLY — one-shot маркер реконсиляции теряет позиции навсегда (2026-07-20)
- Симптом: отчёт `done=25/25 created=19 failed=0`, реально в кабинете 15/25 — 10 позиций пропало, 4 из
  них молча (без строки в error). Инцидент `404c320fc32e`. `auto_requeue_missing.was_created=4`.
- Где: `queue_server.py:_requeue_missing_positions_once` — гейт маркера (было :580
  `if p_res.get("auto_requeue_missing"): return None`) блокировал ВТОРОЙ проход реконсиляции навсегда.
- Root-cause: `missing` = позиции плана, которых нет среди живых имён по loose-матчеру
  (`_position_live_in_names` :681, префикс-матч tp1 / усечение « — »-сегмента tp6/tp7). 4 позиции
  (2× tp1_rsy `aica_newcar-krasnodar` cpc/cpa + 2× tp7/product) loose-матч СЧЁЛ «уже живыми» (ложное
  совпадение / устаревшее состояние кабинета) → выпали из `missing`. Одноразовый маркер сжигался
  безусловно после первой доставки → 4 позиции не доставлялись НИКОГДА, даже если баг вскроется позже.
- Решение (2026-07-20): маркер несёт `attempts`; гейт :580 блокирует только по достижении капа
  `_REQUEUE_MISSING_MAX_ATTEMPTS=3` (env `DIRECT_REQUEUE_MISSING_MAX_ATTEMPTS`). Пока кап не исчерпан —
  свежий проход: `missing` пересчитывается по ЖИВОМУ кабинету, `if not missing: return None` штатно
  останавливает цикл в полном кейсе (не зацикливается); дубли в in-flight-окне отсекает
  `_job_db_active_by_login` (:620). Старый не-dict маркер → прежнее одноразовое поведение (совместимость).
- Детект-запрос (python, `scratchpad/sim.py` crit.2/3): маркер с `attempts=1` НЕ блокирует
  (`gate_blocks=False`) → повторный тик добирает; `attempts>=3` → блок (кап, без вечного цикла);
  `missing=[]` → None до set_marker (полный кейс не крутится). Живой: после доставки число живых
  позиций плана в кабинете должно дойти до `total` за ≤3 тика, а не застрять < total навсегда.
- Побочная находка (репортинг, не чинил): родительские `created`/`done` НЕ сверяются с фактом кабинета
  — `created=19` фантом (оптимистичный bump прерванного прогона + дети, без вычитания недоставленного),
  `done=25` = число проитерированных items (не успехов), `failed=0` обнулён `_reconcile_parent_job_counters`
  (`:714`). Известная граница отчётности: `done/created` НЕ подтверждённый факт кабинета.
- Статус: 🟡 фикс закоммичен, деплой/рестарт за главной сессией, живым повторным прогоном ещё не проверено.
- НЕ помогло ранее: нет — первая правка этой сигнатуры (сама one-shot-слабость задокументирована как
  дизайн в `queue_server.py:616-649`, здесь сработала против нас).

### SLEPOK_VOICE_BU_OVERRIDE_MATRIX_AND_PAVLOV_MB_GENERIC — БУ-конфликты по слепкам + низкий Павлов/Мультибренд (2026-07-19)
- Симптом: вопрос пользователя: если у конкретного слепка голос про новые авто, а сайт `С пробегом`,
  нужен ли отдельный site-type override; отдельно `Тон-войс: Слепок_Павлов/Мультибренд — score 40/100 (mixed)`
  стабильно низкий.
- Где: генерация и аудит голоса — `ai_agents.py` (`SITE_*_OVERRIDES`, `filtered_promo_for_site`,
  `build_*_messages`), `create_set_master_product.py` (tp6/tp7 fallback-пулы), `tools/check_tone_of_voice.py`.
- Root-cause: конфликт был не только у Павлова. В сыром `system`/`promo` БУ-совместимых слепков были
  new-auto маркеры: у `gordeeva`/`kuderko` mixed "новые + б/у", у `tumashenko` позитивные
  `господдержка`/`нулевой утильсбор`, у `terehov` `Гос. поддержка` в `promo.plus`. Для
  `pavlov/Мультибренд` отдельный корень: live job `34524e13b18a` читался как 175 заголовков/48 текстов,
  из них 163 заголовка содержали шаблон `Кредит от 15 банков`, 42 — `Рассрочка`, 27 — `Господдержка`;
  в `direct_slepok_content` для `pavlov/Мультибренд` campaign-title был продублирован 5 раз, а tp6/tp7
  добирал generic credit fallback вместо павловской подписи.
- Решение (2026-07-19): добавлены `SITE_SYSTEM_OVERRIDES` и `SITE_SIGNATURE_OVERRIDES` для
  `gordeeva/С пробегом`, `kuderko/С пробегом`, `tumashenko/С пробегом`, усилен
  `pavlov/Мультибренд`; добавлен `filtered_promo_for_site()` и подключён к promo/campaign/title
  промптам и tone-reference; БУ-фильтр ловит `гос. поддержка`, `новые и б/у`, `с пробегом и новые`;
  tp6/tp7 для `pavlov/Мультибренд` сначала добирает павловскими fallback-пулами
  (`убрали наценку`, выгода в рублях, `одобрение 98%`, `КАСКО на год`, `3 платежа`), generic
  `15 банков` остаётся последним резервом.
- Детект-запрос:
  ```bash
  cd /opt/scripts/home/seoadvanced && DIRECT_ROLE=web PYTHONPATH=/opt/scripts/home/seoadvanced:/opt/scripts/.secret /root/venv/bin/python - <<'PY'
  from direct import ai_agents as A
  from direct.tools.check_tone_of_voice import build_voice_reference
  from direct.create_set_master_product import _pavlov_multibrand_titles
  import re
  conflict_re = re.compile(r"(?i)(господдерж|гос\s*\.?\s*поддерж|госпрограм|утильсбор|новые\s+и|с\s+пробегом\s+и\s+новые|новые\s+авто)")
  for key in ["gordeeva", "kuderko", "pavlov", "tumashenko", "terehov"]:
      sys_text = A.system_for_site(A.AGENTS[key], "С пробегом")
      promo = A.filtered_promo_for_site(A.AGENTS[key], "С пробегом")
      promo_text = " ".join((promo.get("plus") or []) + (promo.get("examples") or []))
      print(key, bool(conflict_re.search(sys_text)), bool(conflict_re.search(promo_text)))
  msg = A.build_titles_messages(A.AGENTS["pavlov"], {"site_type":"Мультибренд","city":"Ставрополь","domain":"x.ru"}, item={"type":"master"}, brand="BAIC")[0]["content"]
  ref = build_voice_reference("pavlov", "Мультибренд")["text"]
  print("pavlov_mb_override_prompt", "Павлов / Мультибренд" in msg)
  print("pavlov_mb_override_ref", "Павлов / Мультибренд" in ref)
  print("pavlov_pool_voice", any("наценк" in s.lower() for s in _pavlov_multibrand_titles("BAIC")))
  PY
  ```
  Норма: все строки БУ-слепков печатают `False False`; `pavlov_mb_override_prompt=True`,
  `pavlov_mb_override_ref=True`, `pavlov_pool_voice=True`.
- Статус: 🟡 фикс задеплоен, ждёт следующего живого tone-check на `pavlov/Мультибренд` и БУ-слепках.
  Старый job `34524e13b18a` перечитывается, но это старый шаблонный контент до фикса; локальный
  LLM-судья через OpenRouter недоступен, поэтому новый score нужно подтвердить новым созданием.

### SLEPOK_VOICE_SITE_TYPE_MISMATCH — голос/судья не учитывали тип сайта (2026-07-19)
- Симптом: tone-watch по `porg-ozge4ntu`, jobs `b90561eebb92` / `58e889fc0c02` / `5f890155b968`
  (`agent=pavlov`, `site_type=С пробегом`) дал 35/100 mixed, 45/100 mixed, 25/100 generic.
  Пользовательский симптом: контент выглядит шаблонным независимо от типа сайта.
- Где: генерация/аудит контента — `ai_agents.py` (`build_*_messages`, `_fanout_head`) и
  `tools/check_tone_of_voice.py::build_voice_reference`.
- Root-cause: `pavlov.site_fit` не включал `С пробегом`, а общая cross-signature Павлова была
  про новые авто. Генератор для БУ запрещал new-auto лексику и оставлял узкий корпус Павлова
  (3 заголовка / 1 текст), no-brand подсказка всё ещё предлагала «новые авто», а tone-check
  сравнивал БУ-контент с общим Павловым new-auto эталоном.
- Решение (2026-07-19): `pavlov.site_fit` расширен на `С пробегом`; добавлены site-type-aware
  `signature_for()` и `filtered_ads_for_site()`; для `pavlov/С пробегом` задан БУ-safe финансовый
  голос; `_fanout_head` и title-промпт больше не дают позитивную инструкцию про «новые авто» для БУ;
  tone-check строит эталон с тем же `site_type`-фильтром и БУ-сигнатурой.
- Детект-запрос:
  ```bash
  cd /opt/scripts/home/seoadvanced && DIRECT_ROLE=web PYTHONPATH=/opt/scripts/home/seoadvanced:/opt/scripts/.secret /root/venv/bin/python - <<'PY'
  from direct import ai_agents as A
  from direct.tools.check_tone_of_voice import build_voice_reference
  ag=A.AGENTS["pavlov"]
  ctx={"site_type":"С пробегом","city":"Ставрополь","domain":"example.ru"}
  msg=A.build_titles_messages(ag,ctx,item={"type":"tp1_rsy"},brand="Lada")[0]["content"]
  ref=build_voice_reference("pavlov","С пробегом")
  print("site_fit_has_bu", "С пробегом" in ag.get("site_fit", []))
  print("voice_ref_has_type", "Тип сайта: С пробегом" in ref["text"])
  print("voice_ref_has_override", "Павлов / С пробегом" in ref["text"])
  print("positive_new_auto_instruction", "новые автомобили в наличии" in msg)
  print("positive_bu_instruction", "авто с пробегом в наличии" in msg)
  PY
  ```
  Норма: `site_fit_has_bu=True`, `voice_ref_has_type=True`, `voice_ref_has_override=True`,
  `positive_new_auto_instruction=False`, `positive_bu_instruction=True`.
- Статус: 🟡 фикс задеплоен, ждёт живого tone-check на следующем создании `pavlov/С пробегом`.
  Старые три job перечитать уже нельзя: `check_tone_of_voice` сейчас даёт `v5=0, grid=0`, черновики
  удалены/недоступны; исторические score сохранены только в `direct_tone_checks` (35/45/25).
- НЕ помогло ранее / не повторять: не лечить только порогом `THRESHOLD` и не считать score валидным,
  если эталон голоса собран без `site_type`.

### LLM_REASONING_MODEL_EMPTY_CONTENT — reasoning-модель отдаёт пустой `content` → тихий статический фолбэк (2026-07-19)
- Симптом: Семён жалуется, что контент объявлений **шаблонный**. В коде — `"OpenRouter: пустой
  content"`, в журнале НИ СТРОКИ, генерация молча собирает контент из корпуса/статики
  (`create_content.py` `fallback=True`). Внешне выглядит как «архитектура такая»: путь-то
  LLM-first, просто LLM «не отвечает».
- Где: транспорт LLM — `llm_providers.py:_consume_sse_stream` (сбор SSE),
  `_or_complete_url` (OpenRouter), точка фолбэка `create_content.py` (`fallback = not good_t and not good_x`).
  Затрагивает ЛЮБОЙ tp, оба пути (token/cookie) — это до создания РК, на генерации контента.
- **Root-cause:** `deepseek/deepseek-v4-flash` (прежний КОД-дефолт `_OPENROUTER_LLM_MODEL`) —
  **reasoning-модель**. Она пишет текст в `delta.reasoning`, а `_consume_sse_stream` собирал
  ТОЛЬКО `delta.content`. На боевом промпте (5835 симв.) весь `max_tokens` съедается
  рассуждением → `finish_reason=length`, `content` пуст. Контролируемая матрица на LXC101
  2026-07-19 (боевой промпт 5835 симв., `max_tokens=280`, по 12 проб на ячейку):

  | модель | top_p=0.9 | без top_p |
  |---|---|---|
  | `deepseek/deepseek-v4-flash` | **6/12 пусто** | **8/12 пусто** |
  | `deepseek/deepseek-chat` | **0/12** | **0/12** |

  То есть v4-flash пуст в **~50-67%** — исходная оценка «~50%» была ВЕРНОЙ. Класс
  **стохастический**: отдельные серии давали 10/10 и 12/12 подряд, но это разброс
  маршрутизации OpenRouter, НЕ детерминизм (проверено повторно). `top_p` ни при чём.
  Поднятие `max_tokens` НЕ лечит (2000 → 6/6 пусто, reasoning разрастается до 3759–4441 симв.).
  Прод-юниты `direct-create`/`direct-create-worker`/`direct-content`/`direct-slepki-worker` имели
  systemd drop-in `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` и НЕ страдали; а
  `direct-copy`, `direct-slepki`, `direct-accounts`, `digest` override НЕ имели → работали на
  сломанном дефолте. Т.е. дефект бил по 4 юнитам из 8 и по любому запуску мимо systemd.
- **Решение (2026-07-19):**
  1. `llm_providers.py`: код-дефолт `_OPENROUTER_LLM_MODEL` → `deepseek/deepseek-chat`.
  2. `_consume_sse_stream` возвращает 3-й элемент `meta` (`reasoning_chars`/`finish_reason`/`chunks`)
     и собирает `delta.reasoning`/`delta.reasoning_content` — раньше эти токены выбрасывались молча.
  3. `_empty_content_class` / `_empty_content_reason`: «пустой content» получил КЛАСС
     (`reasoning_only`/`no_chunks`/`empty_content`) и диагноз с действием; печатается `[llm-or]`
     с номером попытки. Bounded-ретрай (`tries`, backoff) СОХРАНЁН для всех классов, включая
     `reasoning_only` — он стохастический и повтор выигрывает ~в половине случаев.
  4. Счётчик `_LLMDegradeStats` (per-run, сброс в `arm_m3_breaker`): `record_llm_failure` /
     `record_content_fallback` / `log_llm_degrade_summary`. `create_content.py` печатает
     `[content-fallback]` с причиной на КАЖДОЙ упавшей РК; `create_set_orchestrator.py` в `finally`
     печатает сводку `[content-degrade]` за набор (и при аборте тоже).
- **Детект-запрос (LXC101, прод-венв):** доля пустых ответов на боевом промпте + класс причины.
  ```bash
  ssh lxc101-ts "cd /opt/scripts/home/seoadvanced && /root/venv/bin/python3 - <<'PY'
  import sys; sys.path.insert(0,'/opt/scripts/home/seoadvanced')
  from direct import ai_agents as A, llm_providers as L
  ag=A.AGENTS['pavlov']; ctx={'city':'Казань','site_type':'Новые авто','domain':'x.ru'}
  it={'brand':'Haval','city':'Казань','site_type':'Новые авто','tp':'tp1'}
  m=A.build_titles_messages(ag,ctx,item=it,avoid=[],brand='Haval')
  L.arm_m3_breaker('detect'); empty=0; N=10
  for i in range(N):
      t,e=L._or_complete_url('x',m,max_tokens=280,temperature=0.72,top_p=0.9,tries=1)
      if not t: empty+=1
  print(f'model={L._OPENROUTER_LLM_MODEL} EMPTY {empty}/{N}'); print(L.llm_degrade_stats())
  PY"
  ```
  Норма: `EMPTY 0/10`, `llm_failures` пуст. Дефект: `EMPTY>0` и
  `llm_failures={'openrouter:reasoning_only': N}` → в `OPENROUTER_LLM_MODEL` заехала reasoning-модель.
  **Живой мониторинг набора:** `journalctl -u direct-create-worker | grep -E 'content-fallback|content-degrade'`
  — строка `[content-degrade] … на статическом фолбэке=N (X%)` есть в конце КАЖДОГО набора.
- Статус: ✅ подтверждено замером 2026-07-19 — на боевом промпте было 6-8/12 пусто, стало
  **0/12** (и `EMPTY 0/10` детект-запросом); E2E-генерация 2 слепка × 2 типа сайта
  (pavlov/kryuchkova × Новые авто/С пробегом) — `fallback=False` во всех 4 случаях, сводка
  `[content-degrade] РК=4, на статическом фолбэке=0 (0%)`. Прогон создания РК на живом
  аккаунте ещё не делался (деплой за главной сессией).
- НЕ помогло ранее / что НЕ надо делать:
  (а) **увеличение `max_tokens`** — 2000 даёт те же 6/6 пусто, reasoning разрастается
      пропорционально бюджету;
  (б) **крутить `top_p`** — проверено матрицей, на долю пустых НЕ влияет вообще
      (v4-flash 6/12 с top_p=0.9 против 8/12 без него; chat 0/12 в обоих);
  (в) **лечение только systemd-дропином** — так и было сделано ранее для 4 юнитов, но 4 других
      остались на сломанном код-дефолте; чинить надо В КОДЕ, а не только в конфиге;
  (г) ⛔ **НЕ выключать ретрай на `reasoning_only` и НЕ взводить на него OR-breaker.** В этой же
      сессии так и было сделано «потому что класс детерминированный (16/12 подряд пусто)» —
      и это ОШИБКА: контролируемая матрица показала ~50-67%, т.е. серии подряд были разбросом
      маршрутизации OpenRouter. Отключение ретрая на стохастическом классе теряет ~половину
      успешных генераций. Откачено до коммита.
- ⚠ Смежное (НЕ этот дефект, не чинилось): фан-аут 14B фиктивен — в `~/llm/ka_mlx.sh` на M3 блок
  4×14B выключен маркером `#OFF14B`, живёт только 72B на 8086. `M3_LLM_URLS_14B` намеренно НЕ
  задана: с одним инстансом дефолт `[8086]` корректен, а прописать 3 порта = 2/3 сегментов на
  мёртвые порты. Замер: 3 параллельных запроса на 8086 сериализуются (TTFB 1.3/30.4/59.0 с,
  wall 86.4 с ≈ 3×29 с). Включение фан-аута = размен качества (72B→14B), решение Семёна.

### TP67_TARGETING_MODE_BY_NAME — ключи и аудитории tp6/tp7 выбрасывались по ИМЕНИ позиции (2026-07-19)
- Симптом: в структуре слепка у позиции есть ключи и/или аудитории, а в кабинете 0/0, и никто не
  ругается. Факт (джоба `9b2e040edf67`, `porg-ozge4ntu`, слепок `pavlov`, «С пробегом», кампании
  712889267/712889327): ключей 416 → в билде 0 → отправлено 0 → в кабинете 0; аудиторий
  (INTERESTS) 9 → 0 → 0 → 0. Отчёт при этом зелёный.
- Где: `create_set_plan.py:1077` → `create_set_context._tp67_targeting_mode:121-131` (регулярка по
  имени), `create_set_context._slepok_struct_groups:246-264` (перенос полей позиции),
  `create_set_context._tp67_keywords_for` (чтение пака), `create_set_master_product.py:90-107`.
- **Root-cause — ТРИ независимых обрыва, все на этапе ПЛАНА (до отправки):**
  1. **Режим выводился РЕГУЛЯРКОЙ ПО ИМЕНИ** позиции: `re.search(r"автотаргет|автоматическ", text)`.
     «МК - Общая - Автотаргетинг» → `autotarget` → `_has_kw=False`, `_has_aud=False` →
     `interest_ids=[]`, `variant=master_auto`, в `body.items` джобы `"targeting_mode":"autotarget"`.
  2. **`item["audiences"]` вообще НЕ переносился** из структуры в позицию плана — аудитории не
     доезжали даже при верном режиме.
  3. **(найдено при починке, в исходной задаче не значилось)** `_tp67_keywords_for` звал
     `kp.read_keywords(...)` **БЕЗ `group=`** → читался ЛЕГАСИ-файл `{slepok}.txt`, которого у
     per-group слепков нет. Реальные 416 фраз лежат в `pavlov__mk.txt` (слаг из `item["gk"]`,
     `kontent_pack._group_slug`). Т.е. даже с верным режимом ключи бы не поехали.
- **Решение (правило Семёна дословно):** «если в тп6-тп7 нет ключей и аудиторий, то это по
  умолчанию `---autotargeting`, и не важно какое будет название». Режим определяется
  СОДЕРЖИМЫМ структуры, имя позиции на него НЕ влияет:
  - `create_set_context._tp67_modes_from_content()` — есть ключи → `keywords`, есть аудитории →
    `audience`, нет ничего → `autotarget`. Явный `targeting_mode` позиции только ДОБАВЛЯЕТ режим
    (union), чтобы `keyword_source`-гейт продолжал ловить «объявлено КС, а корпус пуст».
  - `_real_keywords()` — `---autotargeting` это МАРКЕР, а не ключ (ключи и автотаргет совместимы:
    Яндекс дополняет ключи автоподбором, маркер остаётся в наборе).
  - `_struct_audience_ids()` + перенос `audiences`/`audiences_unsupported`/`gk` в позицию плана.
    `AUDIENCE:`/`RETARGETING:` — ДРУГИЕ сущности Директа, в UAC `goals` не шлём → считаем
    отдельно и предупреждаем, а не тащим молча.
  - `tp67_struct_expectations()` — эталон СТРУКТУРЫ для позиции (ключи+аудитории+режим), считается
    прямо из структуры/пака; создание (`create_set_master_product`) им же и пользуется.
  - Проверки: `keyword_source`-гейт теперь срабатывает и когда ключи есть В СТРУКТУРЕ
    (`keyword_source` в плане почти всегда `""` и гейт был мёртв); в текст ошибки аудиторий
    добавлен `struct_audiences=N`; НОВАЯ сверка «структура → кабинет»
    `uac_verifier._verify_struct_vs_live` (`UAC_STRUCT_KEYWORDS_MISSING` /
    `UAC_STRUCT_AUDIENCES_MISSING` + `_UNDERCOUNT`), эталон едет в `_res["struct"]`,
    live-счётчики — `uac_read._count_real_keywords` / `_count_audiences` (tri-state).
  ⚠️ Почему НЕ хватило старых проверок: `create_set_master_product.py:130/159` сидели под
  `if _want_keywords` / `if _want_audience` — оба False → код мёртв; `grid_content_verifier.
  _verify_build_vs_live` сверяет build↔live, а build уже пустой → `0 == 0`. Поэтому новая сверка
  идёт от СТРУКТУРЫ, а не от того, что код решил по дороге.
- Детект-запрос (гоняется на LXC101, прод-венв 3.11; локальный 3.9 не тянет синтаксис):
  ```python
  # DIRECT_ROLE=web /root/venv/bin/python3, cwd=/opt/scripts/home/seoadvanced
  from direct import automation_runtime as ar
  csc = ar._create_set_context_module()          # DI: configure() ОБЯЗАТЕЛЕН, иначе NameError _SLEPOK_KEY
  bad = 0
  for d in ar._json("slepki_structure.json")["directologists"]:
      for st in d.get("site_types", []):
          for tp in ("tp6", "tp7"):
              for g in csc._slepok_struct_groups(d["key"], st["name"], tp):
                  exp = csc.tp67_struct_expectations(d["key"], st["name"], tp, "ct0000", "",
                                                     g.get("name"), g.get("sq"))
                  old = csc._parse_targeting_modes((g.get("targeting_mode") or "").strip()
                                                   or csc._tp67_targeting_mode(g))
                  if old == ["autotarget"] and exp["modes"] != ["autotarget"]:
                      bad += 1                    # позиция с содержимым, задавленная в autotarget
  print(bad)
  ```
  **Было: 249** позиций из 599 (12737 ключей + 1805 аудиторий не уезжали). **Стало: 0.**
  Контроль обратной стороны: остаются `autotargeting` 121 позиция, из них с непустым
  содержимым — **0**.
- ⚠️ **Доработка 2026-07-19 (ревью на `c0e7303` вернуло ❌) — ЧАСТИЧНЫЙ ФИКС БЫЛ ХУЖЕ ПОЛНОГО.**
  Первая итерация починила ТОЛЬКО билд (`create_set_master_product`), а план
  (`create_set_plan.py:1077`) остался на регулярке по имени → `_build_name` ставил `ag001`
  (все возрасты), а socdem уезжал `age_35`. **Замер на связке план→билд: 102 tp6-позиции с
  рассинхроном имя↔socdem** (не 249: в `_build_name` tp7 всегда `ag001`, а в билде при
  `is_product` всегда `age_18` → tp7 согласован по построению). Это ТОТ ЖЕ класс, что живой
  инцидент 2026-07-06 `porg-lzjk6p5m`/terehov (см. коммент `create_set_plan.py:83-90`).
  **Правило: режим таргетинга tp6/tp7 обязан считаться ИЗ ОДНОГО источника для плана и билда —
  `tp67_struct_expectations`. Чинить билд, не починив план, ЗАПРЕЩЕНО.** Стало: 0.
  Заодно закрыто:
  - **Матч позиции — по `pos_key`, НЕ по display-имени.** План подставляет город в
    `name/group/label` ДО `position_name`, а структура читается сырой («ТК · ГОРОД») → матч
    промахивался и позиция молча уходила в легаси-файл пака. Поле `pos_key`
    (`{sq}|{группа}|{label|idx}`) подстановкой не трогается. Промах теперь = warning, не молчание.
  - **Сверка структуры на УЖЕ СОЗДАННЫХ РК.** `campaign_result.created_campaigns` отбрасывает
    строки со `skipped`, поэтому на РК, существующих под тем же именем (RESUME-SKIP),
    `UAC_STRUCT_*` не срабатывал НИКОГДА — потерянные ключи оставались потерянными молча.
    Orchestrator кладёт `struct` и в skip-строку, `live_verifier._skipped_struct_rows` даёт по ним
    отдельный проход.
    ⚠️ **Правка круга 2 была ИНЕРТНА, и эта запись раньше утверждала обратное (исправлено).**
    Проход звал `verify_uac_detail`, но деталей по skip-строкам НИКТО не запрашивал: `uac_ids`
    строились тем же `created_campaigns`, который `skipped` выбрасывает (`campaign_result.py:49`)
    → `uac_detail_rows.get(_lid)` всегда `None` → весь проход = счётчик + пачка
    `UAC_DETAIL_SKIPPED`. Ожил только в круге 3: `verification_service._skipped_uac_ids` резолвит
    id по имени из уже прочитанного Grid-снимка (`_grid_rows_by_prefix`, 0 новых запросов) и доливает
    их в `uac_ids` ДО `UacReadClient.campaign_details`.
    🛑 **Предохранитель (круг 3, обязателен вместе с оживлением — порядок именно такой).** Как
    только детали по skip-строкам пошли, ПОЛНЫЙ `verify_uac_detail` на ПРЕД-СУЩЕСТВУЮЩЕЙ кампании
    (созданной не этим прогоном; резолв фильтруется `tp6_`/`tp7_` + полным структурным слагом +
    `FANOUT_SEP`/`_vNN`-нормализацией, поэтому кампанию клиента, сделанную не этим инструментом,
    подцепить нельзя — максимум соседнюю нашу же) поднимает
    `UAC_TITLES_MISSING`/`UAC_SITELINKS_MISSING`/`UAC_COUNTER_MISSING`/`UAC_NOT_DRAFT`, а они ∈
    `repair_gate._UAC_REPLACE_CODES` → `repair_auto.queue_recreate_repair_job` → `delete_uac`.
    То есть skip «не трогать существующее» превратился бы в «удалить существующее». Три рубежа:
    (1) SKIP-проход пропускает наружу ТОЛЬКО `UAC_STRUCT_*` и НИ ОДНОГО repair-кандидата
    (`live_verifier.py`, метка `source="resume_skip"`); (2) `UAC_NOT_DRAFT` ИЗЪЯТ из
    `_UAC_REPLACE_CODES` — «кампания не черновик» больше не даёт права на удаление (детект кода
    остался); (3) `executable_uac_replace_campaigns` отбрасывает всё с `source="resume_skip"`.
    Живой DRAFT-gate в `create_set_repairing._delete_uac_repair_campaigns` (ФИКС-C3) спасал
    только от удаления ЗАПУЩЕННЫХ — пред-существующий ЧЕРНОВИК (созданный не этим прогоном) он
    пропускает.
    🔒 **Круг 4 — изоляция запроса деталей.** Добор skip-id вынесен в ОТДЕЛЬНЫЙ `try`
    (`verification_service.py`, метка ошибки `uac-detail-skip`): в общем `try` отказ по ОДНОЙ
    пред-существующей кампании (нет прав / не видна / Grid отдал мусор) оставлял `uac_details=None`
    → `live_verifier` `if uac_details is not None` ложно → детальная UAC-проверка молча гасла по
    ВСЕМУ набору, включая свежесозданные РК. Теперь основной запрос по `_created_ids` независим.
    ⚠️ **Известная граница (принято осознанно):** внутри skip-части `campaign_details(skip_ids)` —
    ОДИН батч, поэтому один битый id гасит ВСЕ skip-детали (не изолировано по-id). Деградация
    громкая и безопасная: `UAC_DETAIL_SKIPPED` (warn) на каждую позицию + строка `uac-detail-skip:`
    в `live_errors`, путь строго report-only — repair НЕ инициируется, до `delete_uac` не доходит.
    🔀 **Круг 4 — фан-аут по фидам.** `_grid_rows_by_prefix` возвращает ВСЕ совпадения префикса
    (было `_grid_by_prefix` — первое), поэтому позиция, развёрнутая в несколько РК («— feedA»,
    «— feedB»), проверяется целиком, а не по одному sibling'у.
  - `keyword_source`-гейт: ветка `or _struct_exp["keywords"]` УДАЛЕНА как мёртвая
    (`it_keywords` — это и есть `_struct_exp["keywords"]`, внутри `if not it_keywords` она всегда
    ложна). Не держать вид работающей проверки: реальный рубеж — `UAC_STRUCT_*`.
  - Потери аудиторий считаются поэлементно, а не `len(raw)-len(ids)` (дедуп давал ложное
    «не поддержано UAC-путём=N»).
  ❗ **ОТКРЫТО, решение за Семёном:** МЕТКА таргетинга в имени tp6/tp7 («автотаргетинг»/«КС»)
  берётся из ТЕКСТА структуры (`item.t` → display-имя → `_build_name(cat=…)`), а не из
  вычисленного режима. Поэтому позиция «МК - Общая - Автотаргетинг» с 416 реальными КС несёт
  ложную метку (её парсит UI-бейдж). Не правил: это переименование ~249 кампаний (review-first
  + ломает `already_in_direct` по существующим). Варианты: править текст в слепках либо
  переопределять метку в `_build_name` по режиму.
- Статус: 🟡 фикс в коде, живого прогона создания РК НЕ было. Доказано харнессами на прод-венве
  (замеры ниже) + 6 юнит-кейсов новой сверки. Подтверждать живым прогоном на `porg-ozge4ntu`.
- **Детект-запрос (актуальный) — ИСПОЛНЯЕМЫЙ, два скрипта в `direct/`** (раньше здесь была
  словесная инструкция, а рабочий харнесс лежал в scratchpad сессии и был утерян — числа
  воспроизвести было нечем):

  1. `direct/detect_tp67_name_socdem.py` — рассинхрон «имя ⇄ socdem» по связке **план→билд**
     (не в обход `_slepok_struct_groups`: запрос, считающий только режим из структуры, разрыв
     между `_build_name` и `age_lower` структурно НЕ видит).
     ```bash
     ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && \
         DIRECT_ROLE=web /root/venv/bin/python -m direct.detect_tp67_name_socdem'"
     ```
     Прогон 2026-07-19 (после `8a16855`): `позиций tp6+tp7: 599` · **действующий путь: 0** ·
     **«если вернуть вывод режима ПО ИМЕНИ»: 102**. Второе число — величина регрессии `c0e7303`;
     пока оно >0, вывод режима по имени возвращать нельзя.

  2. `direct/detect_tp67_skip_struct.py` — фикстурный (без сети) регресс прохода по RESUME-SKIP:
     резолв id пропущенной кампании + предохранитель от удаления существующих.
     ```bash
     ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && \
         DIRECT_ROLE=web /root/venv/bin/python -m direct.detect_tp67_skip_struct'"
     ```
     Прогон 2026-07-19: `ВСЕ 4 КРИТЕРИЯ ЗЕЛЁНЫЕ` (exit 0) — `_skipped_uac_ids -> [999]` (резолв по
     фид-префиксу), `UAC_STRUCT_KEYWORDS_MISSING expected=416 actual=0`, непрофильных `UAC_*` от
     skip-кампании `[]`, `executable_uac_replace_campaigns -> []`, контроль на своём черновике
     `#111` по-прежнему даёт 1 запись (штатный путь удаления не заглушен).
- НЕ помогло ранее: **починить билд, не починив план** (`c0e7303`) — дало рассинхрон
  имя↔socdem на 102 позициях, состояние ХУЖЕ исходного. ⚠️ Не переизобретать: (1) НЕ возвращать
  вывод режима по имени позиции — это и есть корень; (2) merged-фолбэк
  `_slepok_interest_for_struct` (`source == "fallback"`, объединение ВСЕХ категорий слепка) НЕ
  годится как признак «у позиции есть аудитории»: он непустой почти всегда и делает audience-
  позицией каждую (замер промежуточной версии: 345 позиций и 20103 аудитории вместо 249/1805).
  Брать только аудитории САМОЙ позиции либо совпавшую категорию. (3) **Добавить проход по
  RESUME-SKIP, не добавив их id в `uac_ids`** (круг 2) — проход компилируется, выглядит рабочим и
  не делает НИЧЕГО: деталей нет, всё вырождается в `UAC_DETAIL_SKIPPED`. Проверять оживление
  фактом (`detect_tp67_skip_struct.py`), а не чтением кода. (4) **Оживлять сверку по SKIP БЕЗ
  предохранителя** — полный `verify_uac_detail` на пред-существующей кампании ведёт прямо в
  `delete_uac`. Порядок только такой: сначала фильтр до `UAC_STRUCT_*`, потом оживление.
  (5) **Класть добор skip-id в ОДИН `try` с запросом деталей по своим кампаниям** — один отказ по
  чужому id гасит UAC-проверку всего набора, в логе одна строка `uac-detail`. Только отдельный `try`.


### NEW_CAR_LEXICON_ON_BU_SITE — на Б/У-сайте заголовки про НОВЫЕ авто (фильтр типа сайта был ОДНОСТОРОННИЙ) (2026-07-19)
- Симптом: в кампаниях сайта «С пробегом» (Б/У) живые черновики Мастера кампаний несут заголовки
  про новые авто. Факт с живых черновиков (`porg-ozge4ntu`, слепок `pavlov`, джоба `9b2e040edf67`):
  «Купить новое авто в кредит. КАСКО на 1 год бесплатно» и
  «Выгода до 45% на новые авто. Кредит от 15 банков. Онлайн».
- Где: `automation_runtime.py:1432` (`_GENERIC_AT_TITLES`, строки `:1437`/`:1439`) → путь tp6
  `create_set_master_product.py:185,198` (`title_primary = _GENERIC_AT_TITLES`) → `_cf` (:204).
  Те же строки в `_GENERIC_TEXT_FILLERS:1452` и в `text_gen._RSYA_TEXT_POOL:673,678`.
- **Root-cause — фильтр по типу сайта существовал только в ОДНУ сторону.** `_drop_used_car`
  (`automation_runtime.py:2607`) выкидывает Б/У-лексику, когда сайт НОВЫЙ. Обратного фильтра —
  выкинуть «новое/новые авто», когда сайт Б/У — не существовало НИ В ОДНОМ файле, хотя детект типа
  (`_is_bu_site:2591`) есть с самого начала. Поэтому общий авто-пул (он писан под новые авто)
  протекал в Б/У-кампании целиком.
  ⚠️ Второй слой: в `_rsya_texts` пул-добивка `_RSYA_TEXT_POOL` подмешивалась (`text_gen.py:727`)
  **мимо** `_cf` — т.е. даже правильный фильтр на incoming тексты бы не вычистил.
- Решение, ШАГ 1 (2026-07-19, покрыл ТОЛЬКО tp6/tp7): парный фильтр `_NEW_RE` + `_drop_new_car`
  (`automation_runtime.py`), зеркальный `_drop_used_car`. Подключён там, где работал односторонний
  собрат: `create_set_master_product.py:208` (`_cf`, tp6/tp7) и `text_gen.py:713,937,1152`
  (+ `:735` — пул `_RSYA_TEXT_POOL`, та самая дыра мимо `_cf`).
  DI: `_master_product_deps` + `_tg.configure`.
  ⚠️ **Формулировка «подключён везде» в первой редакции этой записи была НЕВЕРНОЙ и маскировала
  дефект:** «везде, где работал `_drop_used_car`» ≠ «везде, где течёт лексика». Класс оставался
  ЖИВ в tp1–tp5 (см. ШАГ 2) — то есть в БОЛЬШИНСТВЕ кампаний. Урок: писать в журнал охват
  фактом («покрыты такие-то tp»), а не «подключено везде».
  * Словоформы ловятся: «новое/новые/новый/новых/новым/новыми/нового/новому/новой/новую» + «авто»/
    «автомобил», до 2 промежуточных слов («новые китайские авто»).
  * Ложные срабатывания сняты ЯВНО: «новинка» (после «нов» идёт «и», не окончание), «обновление»
    (левая граница `(?<![а-яё])`), «новый год» / «как новый» (нет авто-слова),
    **«новый автокредит» / «новый автосалон»** (`авто(?![а-яё])` — только отдельное слово).
  * **Симметрия доказана конструктивно:** `_drop_used_car` работает при `not _is_bu_site`,
    `_drop_new_car` — при `_is_bu_site`. Условия взаимоисключающие → на любом site_type активен
    максимум ОДИН фильтр, выкосить набор вдвоём они не могут.
  * **Анти-пустой гейт:** `text_gen._NEUTRAL_CREDIT_TITLES` (5 заголовков без Б/У-лексики и без
    «новых авто» → не режутся ни одним из двух фильтров) как floor в `_fallback_master_titles` —
    последний рубеж tp6/tp7 больше не может вернуть `[]` ни на одном типе сайта.
- Решение, ШАГ 2 (2026-07-19, по находкам ревью — закрывает tp1–tp5 и обход валидации):
  * **tp1–tp5, финальная сборка адаптива.** `create_set_assets._upgrade_credit_titles` /
    `_upgrade_credit_texts` вшивали те же строки хардкодом («Купить новое авто. КАСКО на 1 год
    бесплатно», «Выгода до 45% на новые авто…», «Новые авто в кредит. Первый взнос 0 ₽»). Это
    ФИНАЛЬНАЯ точка ПОСЛЕ всех `_cf` — фильтрация выше сюда не достаёт, и `site_type` у функций
    не было вовсе. Протащен `site_type`: `_responsive_ad` → `_upgrade_credit_titles`/`_texts`;
    `variants` обёрнуты в `_drop_new_car` + добор нейтральным пулом. Вызовы:
    `create_set_text_builders.py:135` / `create_set_tp1_builders.py:370` (+ параметр `site_type`
    у `_build_tp2_adgroups`/`_build_tp1_adgroups` и передача из их единственных call-site'ов
    `create_set_text_builders.py:515` / `create_set_tp1_builders.py:1050`).
    DI: `_create_set_assets_deps` (`_drop_new_car`, `_is_bu_site`).
  * **«Новые {brand}» регулярка НЕ ловит и ловить не должна.** `text_gen._title_from_template`
    строит `Новые {brand} в {city}. {promo}` и уходит прямо в ЕПК Title МИМО `_cf`, а `_NEW_RE`
    требует хвост «авто»/«автомобил» — после «Новые» стоит МАРКА («Новые BAIC в Краснодаре» → 0
    матчей). Расширять регулярку на `нов\w+\s+[A-Z]` НЕЛЬЗЯ (срежет легитимное «Новый Haval» на
    сайтах новых авто). Поэтому функция сделана `site_type`-aware: на Б/У префикс → «{brand}
    с пробегом …» (не влез → «{brand} в {city}»), и из промо-пула прокруткой снимается «Скидки на
    новые авто». Прокинут `site_type` из `create_set_text_builders.py:461`,
    `create_set_tp1_builders.py:934,1924`; DI `_is_bu_site` в `_tg.configure`.
    Кандидат `_brand_title_set` «Новый {brand}. Одобрение за 30 минут» → `{brand}. Одобрение за
    30 минут онлайн` **безусловно** (без параметра): функцию зовёт и tp6-путь
    `create_set_master_product.py:175`, который `site_type` не передаёт; слово «Новый» там не
    несло УТП, а brand-first от его снятия только выигрывает.
  * **Floor больше не обходит валидацию.** `_fallback_master_titles` возвращал
    `list(_NEUTRAL_CREDIT_TITLES)[:limit]` голым — мимо `_sanitize_content`/`_trim_to_word`/
    `_bad_ad_title`/`_is_bad_start`/`_variant_norm_key`/`_replace_foreign_city`, т.е. правка любой
    строки пула не ловилась ничем перед отправкой в Директ. Тело цикла вынесено в локальную
    `_collect(src)`, floor идёт через неё же. Гарантия непустоты держится, пока валиден ХОТЯ БЫ
    ОДИН заголовок пула (сегодня валидны все 5); сломать разом все → floor вернёт `[]`, т.е.
    ГРОМКИЙ отказ создания вместо тихой отправки невалидного заголовка в live.
  * Добавлен `_NEUTRAL_CREDIT_TEXTS` (3 текста, ≤81, без «автокредит» — в текстах он запрещён)
    как текстовый близнец пула заголовков.
- Доказательство ШАГА 2 (LXC101, прод-венв 3.11.2, md5 5 файлов Mac==101, read-only; БЫЛО =
  подмена `_drop_new_car` на identity + `_is_bu_site`→False, тот же код-путь):
  * Заголовки Б/У: БЫЛО 7 строк, из них **5 с новое-авто-лексикой** → СТАЛО 7 строк, **0**.
    Все три названные ревью строки убраны. Тексты Б/У: 2 → **0**, комплект 3/3.
  * **Новые авто НЕ тронуты:** «Мультибренд» — заголовки и тексты **побайтово те же**
    (`БЫЛО == СТАЛО: True`), 5 строк с «новыми авто» на месте, как и должно быть.
  * `Новые Haval в Краснодаре` → Б/У: **«Haval с пробегом в Краснодаре»**; новые авто:
    **«Новые Haval в Краснодаре»** (без изменений). То же для BAIC и «Haval Jolion».
    8 подряд вызовов на Б/У → 0 строк с новое-авто-лексикой (промо «Скидки на новые авто» не выдан).
  * Бренд-ветка адаптива: Б/У БЫЛО «Новый Haval Jolion. Выгода до 45%…» → СТАЛО «Haval Jolion
    с пробегом. Выгода до 45%…»; на новых авто «Новый Haval Jolion…» остался.
  * `_brand_title_set("Haval","Краснодар")`: 8 строк, начинающихся с «Нов…» — **НЕТ**, brand-first 7/8.
  * Floor: форсирован (всё, кроме пула, забраковано) → 5/5 на обоих site_type, все проходят
    `_bad_ad_title`/`_is_bad_start`. Гвард-тест: подсунутая в пул запрещённая строка
    «Трейд-ин до 150% цены авто. Без документов» **отсечена валидацией**, floor не опустел (5).
  * Матрица 5 site_type × 5 brand: **пустых наборов 0**, протечек на Б/У 0, комплект 7/3 везде.
    Единственное отклонение — абсурдный `brand='Авито'`: 5 заголовков вместо 7, но это
    **улучшение, а не регресс**: до фикса тот же вход давал `[]` (объявление не создавалось
    вовсе → «пропущено объявление»), нейтральный floor поднял его до 5.
  * `_NEW_RE` после правки `авто(?![а-яё-])`: **16/16** кейсов, «новый авто-кредит» больше не
    ложный матч.
- ⚠️ **Решение Семёна (2026-07-20): тип сайта «Мульти + БУ» — НЕ Б/У-сайт, фильтр `_drop_new_car`
  к нему НЕ применять.** Живая проверка `karavaev`/«Мульти + БУ» (`porg-ozge4ntu`, джоба
  `207b53c47f27`) нашла кампанию `712901018` («Поиск_Автокредит_бу», явно Б/У по названию) с живым
  текстом «новое авто. Кредит и первый взнос 0 ₽» — выглядело противоречиво. Уточнил у Семёна:
  весь сайт «Мульти + БУ» трактуется как сайт НОВЫХ авто целиком (`_is_bu_site` уже возвращает
  `False` для этого значения `site_type` — `automation_runtime.py:2610-2612`, строгое сравнение
  `== "С пробегом"`). **Это НЕ баг, правка НЕ нужна** — код уже ведёт себя так, как решил Семён.
  Не путать с per-модельной фильтрацией (обсуждалась и отклонена: усложнила бы код ради случая,
  который решением Семёна закрыт на уровне типа сайта).
- ⚠️ **Известные ГРАНИЦЫ `_NEW_RE`** (осознанный объём, НЕ баг — чтобы при следующей протечке не
  искали баг в регулярке): не ловятся синонимы без слова «авто/автомобиль» — «новые машины»,
  «новый кроссовер», «новые модели», «новые иномарки» — и инверсный порядок «Автомобили новые
  в наличии». В текущих пулах таких строк нет. Появятся — чинить точечной строкой пула или
  явным `site_type`-ветвлением, НЕ расширением регулярки вслепую (`нов\w+\s+[A-Z]` начнёт резать
  легитимное «Новый Haval» на сайтах новых авто).
- Доказательство (LXC101, прод-венв 3.11.2, md5 3 файлов Mac==101, read-only, БЫЛО = подмена
  `_drop_new_car` на identity — тот же код-путь, те же данные):
  * **tp6 ct0000 сборка целиком** (репродукция `create_set_master_product.py:225-253`),
    `pavlov`/«С пробегом»: БЫЛО 5 заголовков, среди них ровно две живые дефектные строки
    «Купить новое авто в кредит. КАСКО на 1 год бесплатно» и «Выгода до 45% на новые авто. Кредит
    от 15 банков. Онлайн» → СТАЛО 5 заголовков, обе строки ушли, на их место встали
    «Трейд-ин выше рынка. Платеж от 9 000 ₽/мес в кредит» и «Одобрение за 30 минут. Кредит на авто
    от 15 банков». **Комплект 5/5 сохранён.**
  * Пулы Б/У: `_GENERIC_AT_TITLES` 8→5, `_GENERIC_TEXT_FILLERS` 4→3, `_RSYA_TEXT_POOL` −2 строки.
  * **Обратный случай НЕ сломан:** «Мультибренд» (новые авто) — во ВСЕХ 8 замерах убрано 0 строк,
    tp6-сборка побайтово та же (обе «дефектные» строки на месте, как и должно быть).
  * **Пустых наборов: 0** (8 замеров × 2 site_type). Протечек `(?i)нов(ое|ые|ый|ых)\s+авто`
    в Б/У-контенте: **0**.
- ⛔ **Фикс действует ТОЛЬКО на БУДУЩИЕ прогоны — уже созданные РК он НЕ чинит.** В
  `campaign_spec_audit.py` issue-кода под эту лексику НЕТ (проверено grep: есть `SHORT_TITLES`,
  `BRAND_NOT_FIRST`, `CONTENT_TEXTS_LOW` и др., но ничего про «новые авто»), значит delayed
  content_repair такие заголовки не увидит и не перезальёт. Живые Б/У-черновики (напр.
  `porg-ozge4ntu`, джоба `9b2e040edf67`) останутся с дефектными строками до пересоздания или
  ручной правки. Нужен авто-ремонт существующих — заводить отдельный issue-код в аудите.
- 🔎 **ДЕТЕКТ: ТОЛЬКО РУЧНОЙ (автоматического нет, и это осознанно, а не забыли).**
  Поля «детект-запрос» у этого класса нет и быть не может в текущей схеме: **контент объявлений
  в БД джоб не хранится** — ни заголовки, ни тексты. Проверить можно ИСКЛЮЧИТЕЛЬНО live-чтением
  из Директа. Готового скрипта **нет**: он требует Grid-куки конкретного аккаунта, а писать
  непрогнанный live-скрипт в репозиторий = отдать неверифицированный код. Порядок ручной
  проверки — ниже; при автоматизации оформить как отдельный скрипт на `grid_read.py` и прогнать
  на `porg-ozge4ntu` ПЕРЕД коммитом.
- Ручная процедура детекта: live-чтение
  заголовков кампаний Б/У-джобы со счётом матчей `(?i)нов(ое|ые|ый|ых)\s+авто` (>0 = дефект).
  Аккаунт `porg-ozge4ntu`, слепок `pavlov`, «С пробегом», джоба `9b2e040edf67`: читать заголовки/
  тексты адаптивных объявлений кампаний джобы через Grid (`grid_read` / `GridClient`, тот же путь,
  которым ходит `grid_content_verifier`) и считать матчи регулярки по всем `titles`+`texts`.
  `new_car_matches > 0` = дефект; норма Б/У-кампании = 0.
  ⚠️ Готовым скриптом НЕ прогонялся: на момент фикса живые черновики джобы не читались, точные
  имена методов читателя под этот аккаунт сверить по `grid_read.py` перед запуском.
- Статус: 🟡 фикс на Mac+LXC101 (md5 совпали), доказан офлайн на реальных пулах и реальной tp6-сборке;
  **живой прогон создания Б/У-кампании НЕ гонялся** — ждёт прогона. НЕ деплоено (сервисы не рестартовались).
- Чинит ли уже созданные РК: **НЕТ.** Фильтр работает только на ГЕНЕРАЦИИ контента (будущие прогоны
  create_set). Delayed content_repair (`campaign_spec_audit` → keywords/brand/sitelinks repair) этот
  класс не покрывает — кода `NEW_CAR_ON_BU_LIVE` в аудите нет, дефект в живых черновиках
  `9b2e040edf67` останется до пересоздания.
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: фильтр НЕ должен резать
  «нов*» само по себе — «новый автокредит», «новинка», «Новый год», «как новый» легитимны; режем
  ТОЛЬКО связку «новый+авто/автомобиль». И не глушить пустотой: floor `_NEUTRAL_CREDIT_TITLES`
  обязан оставаться нейтральным к ОБОИМ фильтрам, иначе анти-пустой гейт сам себя обнулит
  (прецедент — `FOREIGN_MODEL_FILTER_EMPTIES_ADGROUP`, где «непустой» список состоял из спецключа).
- ⚠️ **ШАГ 1/2 закрывали НЕ ВСЁ — формулировки «подключён там, где работал собрат» и «закрывает
  tp1–tp5» были неполными.** Класс остался ЖИВ на слепке `kryuchkova` (тот же аккаунт
  `porg-ozge4ntu`, «С пробегом»): live-детект нашёл **36 совпадений** «новых авто» в 4 кампаниях
  («Купить новое авто в кредит…», «Выгода до 45% на новые авто…»). Павлов прошёл СЛУЧАЙНО — его
  тонкие группы добились до cap уже отфильтрованным контентом; условие ТЕЧИ = Б/У-сайт + группа,
  чей контент после `_cf` просел ниже cap → срабатывает СЫРОЙ (мимо `_cf`/`_drop_new_car`) добор.
  Затрагивает ЛЮБОЙ из 12 Б/У-слепков (`avto_sk avtolajt_bu gen_ses kryuchkova kuderko gordeeva
  salamahin pavlov karavaev sk_krs tumashenko terehov` — все имеют site_type «С пробегом»): пулы
  глобальные (`_GENERIC_*` в `automation_runtime`), билдеры slepok-agnostic, дыра системная.
- Решение, ШАГ 3 (2026-07-20 — четыре сырых generic-хвоста + дырявый «последний рубеж»):
  * `text_gen.py:1229` — хвост `_cf(supp) + _GENERIC_TITLE_FILLERS` (сырой `_GENERIC_TITLE_FILLERS`
    приклеен ПОСЛЕ `_cf`) → `+ _cf(_GENERIC_TITLE_FILLERS)`.
  * `text_gen.py:~1283` — финальный фолбэк тонкой не-брендовой группы
    `for cand in (_GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS)` (СЫРОЙ пул, источник обеих живых
    строк) → `for cand in _cf(_GENERIC_AT_TITLES + _GENERIC_TITLE_FILLERS)`.
  * `create_set_master_product.py` (коммит 38b02f0, база HEAD — БЕЗ чужой незакоммиченной
    pavlov-работы из рабочего дерева): заголовки `_cf(title_supp) + ([] if _is_non_auto else
    _GENERIC_TITLE_FILLERS)` (сырой `_GENERIC_TITLE_FILLERS` мимо `_cf`) → `_cf(title_supp) +
    ([] if _is_non_auto else _cf(_GENERIC_TITLE_FILLERS))`; тексты `tpl_texts + ([] if _is_non_auto
    else _GENERIC_TEXT_FILLERS)` → `_cf(tpl_texts) + ([] if _is_non_auto else _cf(_GENERIC_TEXT_FILLERS))`.
  * `create_set_tp1_builders.py:~980` — фолбэк «Товарная галерея»
    `list(_GENERIC_AT_TITLES)`/`list(_GENERIC_TEXT_FILLERS)` (вообще без фильтра) → предвычисляются
    `_drop_new_car(list(...), site_type)`, из них берутся `titles/texts/title/text`.
  * **«Последний рубеж» сделан НАСТОЯЩИМ.** `create_set_assets._upgrade_credit_titles` (стр. ~348)
    и `_upgrade_credit_texts` (стр. ~452) резали `_drop_new_car` только СВОЙ хардкод `variants`, а
    входящий `seq` уходил в live как есть — и в early-return, и в цикле `variants + seq`/`seq +
    variants`. Добавлен `seq = _drop_new_car(seq, site_type)` в САМОМ начале обеих функций, ДО
    ветвления → обе ветки чисты. Анти-пустой держится: `_needs_credit_title_upgrade([])` → True →
    апгрейд-ветка с floor `_NEUTRAL_CREDIT_TITLES`/`_TEXTS`; у текстов `_credit_title_anchor(seq or
    ["Авто в кредит"])` уже страхует пустой seq.
  * `_NEW_RE` НЕ трогали (правило прошлых кругов) — только подключили существующий `_drop_new_car`
    к новым точкам.
- Доказательство ШАГА 3 (Mac, py3.12, offline harness на пулах/функциях, скопированных verbatim
  из `automation_runtime`; БЫЛО показано через обратный случай — те же строки на новом сайте):
  * `text_gen._rsya_titles` non-brand тонкая группа: Б/У 7 строк **new_present=False** (обе живые
    строки ушли, набор не пуст) ↔ «Легковые новые» 7 строк **new_present=True** (те же строки на
    месте, обратный случай не сломан).
  * Пулы на Б/У: `_GENERIC_AT_TITLES` 8→5, `_GENERIC_TITLE_FILLERS` 7→5, `_GENERIC_TEXT_FILLERS`
    4→3, все new=False; на новом сайте 8→8/7→7/4→4, new=True (no-op).
  * `_upgrade_credit_titles` с seq из 2 живых строк: Б/У 7 строк, обе ушли, не пусто ↔ новый сайт
    сохраняет. `_upgrade_credit_texts`: Б/У 3/3 без «новых авто» ↔ новый сохраняет.
  * **Анти-пустой:** seq ЦЕЛИКОМ из новых-авто на Б/У → titles n=7, texts n=3, **не []**, new=False.
  * `py_compile` всех 4 файлов — OK.
- ⛔ **ШАГ 3, как и 1/2, действует ТОЛЬКО на БУДУЩИЕ прогоны.** Issue-кода под эту лексику в
  `campaign_spec_audit.py` по-прежнему НЕТ → delayed content_repair 36 живых строк `kryuchkova`
  НЕ перезальёт. Живые Б/У-черновики остаются до пересоздания. Авто-ремонт существующих = отдельный
  issue-код в аудите (не заводился).
- ⚠️ НЕ верифицировано ШАГом 3: живой прогон создания Б/У-кампании; повторный live-детект на
  `kryuchkova` (джоба `0d28734e3c4d`) — требует Grid-куки, гонит главная сессия/`direct_verifier`.

### SITELINK_HREF_ANCHOR_LEAKS_LIVE — внутренний якорь `#slN` уезжает в живой Href быстрой ссылки (2026-07-19)
- Симптом: у быстрых ссылок созданных РК Href вида `https://<домен>#sl1` / `#credit` / `#banks` —
  якорь ведёт в никуда (раздела на сайте нет). Ошибок создания НЕТ, дефект молчаливый.
  Аккаунт `porg-ozge4ntu`, слепок `pavlov`, «С пробегом», джоба `9b2e040edf67`.
- Где: сборка — `create_set_feed_builders.py:59` (`_ensure_sitelink_hrefs`, `base + f"#sl{i+1}"`)
  и статрезерв `:25-30,41` (`frag`). Отправка — Grid `grid_finalize.py:add_sitelink_set` и
  v5/v501 `direct_v501_client.py:add_sitelinks_set`.
- Root-cause: якорь введён НАМЕРЕННО как обход «Grid AddSitelinkSets не любит дубли href»
  (нужен уникальный href на каждую ссылку). Но он проставлялся НА ЭТАПЕ СБОРКИ и дальше
  ничем не снимался → доезжал до живого объявления.
- Решение (решение Семёна: «якорь это ошибка, нужно снимать якорь непосредственно перед
  отправкой»): якорь остаётся внутренним средством различения на сборке, а в ТОЧКЕ ОТПРАВКИ
  режется. Новый `text_norm._strip_href_fragment` вызывается в обоих send-point'ах:
  `grid_finalize.py` (`add_sitelink_set`, поле href) и `direct_v501_client.py`
  (`add_sitelinks_set`, поле `Href`). Href из одного фрагмента не трогаем (пустой Href
  Директ отбивает валидацией и теряет весь набор). 2026-07-19.
- Детект-запрос (живой Href с якорем в созданных РК):
  ```sql
  WITH sl AS (SELECT DISTINCT j.login, jsonb_path_query(j.result,'$.**.href') #>> '{}' AS href
              FROM public.direct_automation_jobs j
              WHERE j.result IS NOT NULL AND j.created_at > now() - interval '30 days')
  SELECT count(*) FILTER (WHERE href ~ '#(sl[0-9]+|credit|banks|trade-in|no-first-pay)') AS anchor_hrefs,
         count(*) AS total_hrefs FROM sl WHERE href LIKE 'http%';
  ```
  Плюс live-проверка набора на аккаунте (Grid `get_sitelink_sets` / v5 `sitelinks.get`).
- Статус: 🟡 ждёт прогона. ⚠️ **НЕ проверено живьём: примет ли Grid набор с ОДИНАКОВЫМИ href**
  (после срезки все ссылки набора ведут на один base). Претензия «Grid не любит дубли href» —
  только код-комментарий, в журнале подтверждения нет; UAC-дедуп бьёт по description
  (`DUPLICATE_SITELINK_DESCS`), не по href. Первый живой прогон обязан посмотреть на
  `add_sitelink_set` → `validationResult.errors`. Отобьёт — сюда пишем ❌ и не изобретаем
  обход вслепую.
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: **удалять** якорь
  на этапе сборки НЕЛЬЗЯ — он там нужен для различения ссылок; снимать только перед отправкой.

### CAMPAIGN_NAME_SEGMENT_DUPES — повтор сегмента в имени РК (обобщение, 2026-07-19)
- Симптом (живая проверка кабинета `porg-ozge4ntu`, джоба `b90561eebb92`, слепок pavlov/«С пробегом»):
  6 из 12 товарных tp7 — `… — ТК - Автосалон - ТК - Автосалон - Автотаргетинг - Ставропольский край — …`,
  `ТК - Автокредит - Ключевики - ТК - Автокредит - КС`, то же для Авито/Дром/Автору. tp5 —
  `Товарная галерея  - Товарная` (двойной пробел). Верификатор дал `lv_errors=0` — класс не детектился.
- ТРЕТИЙ случай ОДНОГО класса: (1) домен фида дважды (`ecd1ae8`), (2) «Мастер кампаний - Мастер
  кампаний» (`create_set_plan.py:92`), (3) «ТК - Автосалон» дважды. Каждый раз чинился ЛИТЕРАЛ.
- Root-cause — **в КОДЕ, не в данных**: имя собирается слепой склейкой двух независимых
  человеческих ярлыков одной позиции — `group.name` и `item.t`
  (`create_set_context.py:322-324`, `display = f"{gname} - {label}"`), плюс метка типа
  (`create_set_plan.py:_build_name`), область и метка фида. Ярлыки в слепках СПЛОШЬ пересекаются
  (220 позиций из 14894 по 11 слепкам: «ТК - Автосалон»+«ТК - Автосалон - Автотаргетинг»,
  «МК»+«МК - Общая - …», «Lada»+«ТК - Lada - …») — это нормальные ярлыки кабинета, каждый новый
  харвест их воспроизведёт. Гард был только один и частный: `label ⊂ gname`, и `cat.startswith(tp_label)`.
- Решение (2026-07-19): ОДИН общий механизм `create_set_context.dedup_name_segments(name)` —
  сегмент (разделитель ` - `, чанки ` — `) не приклеивается второй раз, если такой уже есть в
  собираемом имени (регистро/пробело-независимо, NBSP и двойные пробели нормализуются). Литералы
  не зашиты. Точки применения (5, все продакшн-пути сборки имени):
  1. `create_set_plan._build_name` — вместо частного `startswith(tp_label)` (tp6/tp7);
  2. `create_set_plan._uniq` — единая воронка ПЛАНОВЫХ имён всех tp, после приклейки ОБЛАСТИ
     (для tp7 метка фида тоже в плане, до `_uniq` → покрыта здесь);
  3. `create_set_master_product` после приклейки `feed_label` (парити имя-план == имя-билд);
  4. `create_set_feed_builders._create_tp5_campaign` / `_create_tp3_campaign` (API-путь) и
  5. `create_set_feed_builders` cookie-путь tp5.
  ⚠️ Круг 1 (`fff5c8d`) закрыл только 1-3 и УТВЕРЖДАЛ, что `_uniq` — воронка ВСЕХ tp «уже после
  приклейки метки фида». Для **tp3/tp5 это было неверно**: их метка фида клеится ПОЗЖЕ и в ДРУГОМ
  модуле (`create_set_feed_builders`), `_uniq` её не видит. Точки 4-5 добавлены кругом 2. Не
  полагаться на то, что «дедуп в `_uniq` = дедуп финального имени» — для товарных tp3/tp5 нет.
- Детект-запрос (обобщённый, без литералов — «сегмент повторяется дважды»):
  ```sql
  WITH nm AS (SELECT DISTINCT jsonb_path_query(j.result,'$.**.name') #>> '{}' AS name
              FROM public.direct_automation_jobs j
              WHERE j.result IS NOT NULL AND j.created_at > now() - interval '30 days'),
  seg AS (SELECT name, lower(btrim(s)) AS s
          FROM nm, regexp_split_to_table(regexp_replace(name,'\s+',' ','g'), ' [-—] ') AS s
          WHERE name LIKE 'tp%')
  SELECT count(DISTINCT name) AS names_with_dupe_segment
  FROM (SELECT name, s, count(*) c FROM seg WHERE s <> '' GROUP BY 1,2 HAVING count(*) > 1) d;
  ```
  Оффлайн-детект по структуре (без БД) — в репозитории: `direct/detect_name_segment_dupes.py`
  (воспроизводимый счётчик; собирает ПЛАНОВЫЕ имена всех tp, а для tp3/tp5/tp7 — ФИНАЛЬНЫЕ с
  зонд-меткой фида, сравнивает с `dedup_name_segments`). Прогон 2026-07-19 (прод-венв 3.11,
  2125 плановых кампаний): ПЛАНОВЫЕ имена с повтором **483 → 0**; ФИНАЛЬНЫЕ tp3/tp5 под зондом
  «worst» (метка = последний сегмент имени, худший случай, реально бывший на cookie-tp5)
  **162 → 0** (из них tp3 59, tp5 103), различимых имён схлопнуто **0**; под зондом «realistic»
  (метка = URL фида, как строит код) 0 → 0. Прежняя цифра «988» из круга 1 была померена на
  ПЛАНОВЫХ именах и невоспроизводима — заменена этим скриптом.
  В live-верификатор добавлен код `NAME_SEGMENT_DUPE` (report-only, 0 запросов,
  `grid_content_verifier.py`) — сравнивает живое имя с тем же дедупом.
- ⚠️ ОСОЗНАННАЯ ПОТЕРЯ (принятое поведение, не баг): дедуп режет ПОВТОРНОЕ вхождение сегмента,
  включая ХВОСТОВОЕ. `ТК - Москва - Автотаргетинг - Москва` → `… - Автотаргетинг` (хвостовой
  регион исчезает; такие хвосты реальны — «Краснодарский Край», «Волгоградская Область»,
  «Ханты-Мансийский автономный округ»). Так же теряется хвостовой ярлык из `item.t`:
  `ТК - Дилер - Ключевики - ТК - Общая - КС - Дилер` → `ТК - Дилер - Ключевики - Общая - КС`.
  Безопасно: коллизий на всей структуре нет (различимых схлопнуто 0), регион уезжает через
  `r_code` кодера (не из имени), UI-бейдж парсит `groupName`. Докстринг
  `create_set_context.dedup_name_segments` поправлен (был ошибочный «регион не пропадает»).
- Статус: 🟡 ждёт живого прогона (offline по всей структуре, ПЛАН+ФИНАЛ tp3/tp5, пройден; live не гонялся).
- ⚠️ Побочный эффект (нужно решение Семёна): `kryuchkova`/Монобренд/tp1 держит ДВА camp_names,
  различающихся ТОЛЬКО двойным пробелом («РСЯ - Модели  - Автотаргетинг» и «РСЯ - Модели -
  Автотаргетинг», аналогично «- КС»). После нормализации пробелов они СХЛОПЫВАЮТСЯ в одно имя
  (16 позиций). Похоже на опечатку данных, но это структурное слияние — не применять молча.
- НЕ помогло ранее: частные условия на литерал («Мастер кампаний», `tp_label` через `startswith`,
  гард `label ⊂ gname`) — каждый ловил ровно один литерал и пропускал следующий. Не заводить
  четвёртое частное условие: класс чинится ТОЛЬКО общим дедупом на сборке имени.
- ИЗВЕСТНАЯ ГРАНИЦА `NO_IMAGES_LIVE` (не трогали — риск ложных срабатываний): гейт детекта
  `tp == 1` (`grid_content_verifier.py:207`), из-за чего остаток 1/40 (tp5 `712894005`) молчит.
  Расширять на tp5 НЕ стали: `NO_IMAGES_LIVE` считает адаптивные ТЕКСТ-объявления `GdAdaptiveTextAd`
  без `imageHash`, а у tp5 комбинаторные адаптивные объявления по дизайну ТЕКСТ-ONLY (картинки/видео
  живут у `ShoppingAd`/`ListingAd`, `create_set_tp1_builders.py:442-443`). Пути билдера tp5
  противоречивы (`_tp1_pack_groups` кладёт `image_hashes`, адаптивный repair их для tp5 пропускает),
  поэтому расширение гейта на tp5 могло бы ложно флагать КАЖДУЮ tp5-кампанию. Нужен доменный разбор
  «где реально должна лежать картинка tp5» до правки гейта — не слепое расширение.

### CAMPAIGN_NAME_DOMAIN_AND_TP_LABEL_DUPES — домен фида и «Мастер кампаний» дважды в имени РК (2026-07-19)
- Симптом: за 30 дней 15 имён кампаний содержат домен сайта (`… — carsklad-126.site/yandex-…xml`),
  2 имени содержат `Мастер кампаний - Мастер кампаний`. Из 62 tp-имён. Аккаунт `porg-ozge4ntu`.
- Где: домен — `create_set_feed_builders.py:995` (tp5), `:1197` (tp3), `create_set_plan.py:1113`
  (tp7), `create_set_tp1.py:125` (tp1, гард `_skip_domain_fname`). Дубль метки —
  `create_set_plan.py:_build_name:92` (`tp_label`) + `create_set_context.py:243-245`
  (`display = f"{gname} - {label}"`, где `gname` уже = «Мастер кампаний»).
- Root-cause:
  1. **Домен:** в имя подставляется URL фида без схемы (`_f_label`), а гард
     `_is_site_domain_name(feed_name, href)` сверял с хостом ДРУГОЕ значение — короткое имя
     фида из кабинета, и только на ТОЧНОЕ равенство. tp1 — тот же дефект: имя фида в кабинете
     `carsklad-126.site — yandex-catalog-…`, хост идёт ПРЕФИКСОМ и проходит гард.
  2. **Двойная метка:** `_build_name` всегда клеит `tp_label`, а `cat` (имя позиции слепка) уже
     начинается с него. Гард на `create_set_context.py:244` проверял только `label ⊂ gname`,
     обратное — нет.
- Решение (2026-07-19): новый `model_urls._strip_site_domain_label(label, href)` (+ выделен
  `_href_host`) — режет домен-ПРЕФИКС из ТОГО, что реально уходит в имя, пусто на выходе =
  суффикс не добавлять. Подключён в tp5/tp3/tp7/tp1 вместо прежних гардов
  (`_skip_domain_fname` заменён на `_feed_name_label`). В `_build_name` — дедуп: `cat`
  начинается с `tp_label` → метку второй раз не клеим. Различимость fan-out цела: после
  срезки остаются разные суффиксы (`yandex-catalog-model-design.xml` vs `yandex-used-auto.xml`),
  tp7 дополнительно защищён `_uniq`.
- Детект-запрос:
  ```sql
  WITH nm AS (SELECT DISTINCT j.login, jsonb_path_query(j.result,'$.**.name') #>> '{}' AS name
              FROM public.direct_automation_jobs j
              WHERE j.result IS NOT NULL AND j.created_at > now() - interval '30 days')
  SELECT count(*) FILTER (WHERE name ~ ' — [a-z0-9-]+\.(site|ru|com)') AS domain_in_name,
         count(*) FILTER (WHERE name ~ 'Мастер кампаний - Мастер кампаний') AS mk_doubled,
         count(*) AS total_names
  FROM nm WHERE name LIKE 'tp%';
  ```
  Было `15 / 2 / 62`; после правки на ТЕХ ЖЕ (старых) джобах — те же `15 / 2 / 62` (запрос
  считает историю, новые числа даст только свежий прогон).
- ⚠️ ДОРАБОТКА 2026-07-19 (проверяющий вернул ❌): правка ПЛАНА домен НЕ убирала — имя
  пересобиралось на БИЛДЕ. `create_set_plan.py:1168` клал в item СЫРОЕ `feed_name` из кабинета,
  а `create_set_master_product.py:663` клеил его вторым суффиксом поверх уже очищенной плановой
  метки, сверяясь тем самым старым гардом на точное равенство. Живой результат:
  `… — yandex-catalog-model-design.xml — carsklad-126.site — yandex-catalog-model-design`
  (домен + дубль) и вдобавок имя ПЛАНА ≠ имени БИЛДА → рассинхрон resume/`already_in_direct`
  (класс `FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC`). Второй пропущенный путь — **tp5 cookie**
  (`create_set_feed_builders.py:530`, `feed_name` из `_grid_feeds`), чинился только API-путь.
  Фикс: обе точки переведены на `_strip_site_domain_label(…, href)`; домен из имени ушёл.
  Плюс `model_urls.py:119` — `lstrip` по НАБОРУ символов заменён на срез РОВНО ОДНОГО
  разделителя (съедал ведущий дефис метки `-catalog.xml`).
- ⚠️ ДОРАБОТКА №2 2026-07-19 (проверяющий вернул ❌ повторно): **равенство имён план↔билд, которое
  тут ранее записали как достигнутое, было СОВПАДЕНИЕМ ДАННЫХ, а не свойством кода.** План считал
  метку из **URL** фида, билд — из **имени фида в кабинете**; проверка `label not in disp_name`
  гасила дубль только пока имя кабинета оказывалось case-exact ПОДСТРОКОЙ имени файла. На реальных
  данных это ломается: кабинет `Фид легковых` / `Yandex-Used-Auto` (иной регистр) / `feed-2024`
  → возвращались дубль-суффикс и рассинхрон `already_in_direct`.
  Фикс (равенство ПО ПОСТРОЕНИЮ): план кладёт в item ту же метку, что подставил в имя —
  `"feed_label": _f_lbl` (`create_set_plan.py:1167`); билд берёт `it.get("feed_label")`
  (`create_set_master_product.py:670`) и пересчитывает из `feed_name` ТОЛЬКО при отсутствии
  ключа (item не из плана, путь «Fix A»). Плюс `model_urls.py:124`: срез разделителя выполнялся
  и когда хост-префикс НЕ найден (`'— catalog.xml'` → `'catalog.xml'`) — теперь только внутри
  ветки с реально отрезанным префиксом.
- ⚠️ Почему дефект проскакивал проверки: (1) первое доказательство дёргало функции нейминга
  **плана**, билд-путь в него не входил; (2) расширенная версия брала фикстуры, у которых имя
  кабинета — подстрока имени файла, поэтому `assert plan == build` не мог упасть В ПРИНЦИПЕ.
  `.claude/sdd/naming-anchor-fix-proof.py` теперь: фикстуры с человеческим именем/иным регистром/
  иным суффиксом + состав ключей item читается из РЕАЛЬНОГО исходника плана (`inspect.getsource`),
  чтобы модель не доказывала равенство сама себе. **Правило: правку нейминга проверять на ОБОИХ
  уровнях (план И билд) и на фикстурах, где наивная реализация ОБЯЗАНА падать.**
- Статус: 🟡 ждёт прогона. Доказано юнит-вызовом функций нейминга (имя ДО/ПОСЛЕ для tp5/tp1/tp6,
  tp7-билд и tp5-cookie, совпадение имён план↔билд на 5 парах включая «Фид легковых» и иной
  регистр, fan-out без схлопывания). Расширенный proof падает на старом коде (assert «билд
  добавил лишний суффикс») и проходит на новом. Живого прогона создания РК НЕ было.
- НЕ помогло ранее: **правка ТОЛЬКО на уровне плана (коммит `54b4c04`) — НЕ помогает**: билд
  пересобирает имя из `it["feed_name"]` и возвращает домен. ⚠️ Не переизобретать: гард на ТОЧНОЕ
  равенство имени фида хосту (`_is_site_domain_name`/`_skip_domain_fname`) этот класс НЕ ловит —
  сверять надо с реально подставляемой меткой и резать префикс. ⚠️ Также НЕ помогает: **считать
  метку на билде НЕЗАВИСИМО от плана** (пусть даже той же функцией `_strip_site_domain_label`) —
  входные данные разные (URL vs имя кабинета), равенство держится только на удачных данных.
  Метку обязан передавать план (`feed_label`), билд — потреблять, не пересчитывать.

### FEED_FALLBACK_PLAN_VS_BUILDER_DESYNC — фолбэк-фид применяется на БИЛДЕ, но не в ПЛАНЕ → tp7 теряется тихо (2026-07-19)
- Симптом: товарные кампании tp7 **не создаются вообще** — их нет в плане, хотя в структуре
  слепка (`pavlov` / «С пробегом») их 12. Ошибок нет, отчёт зелёный — дефект МОЛЧАЛИВЫЙ.
  Факт по джобе `9b2e040edf67`: `single_feed=True, feed_confirmed=True, single_feed_fallback=None`,
  в `items` НЕТ типа `product`. Все QA-прогоны шли без tp7.
- Где: `create_set_plan.py:468` (гейт `single_feed_fallback or feed_confirmed`) и `:516`
  (`feeds = []` → `_emit_struct("tp7")` даёт 0 product-item'ов); `slepok_qa_run.py:306` (body_plan);
  `static/direct/automation.js:1843/3119` (ветка `dec==='confirmed'`).
- **Root-cause — рассинхрон уровня, на котором резолвится фид:**
  * **tp7** резолвит фид на этапе **ПЛАНА**. Профильных фидов (`/yandex.xml`,
    `/yandex-used-auto.xml`) на аккаунте нет → фолбэк применяется, ТОЛЬКО если в теле `/set_plan`
    пришёл `single_feed_fallback` ИЛИ `feed_confirmed`. Не пришёл → `feeds=[]` → tp7 нет в плане.
  * **tp5/tp3/tp1-товарка** резолвят фид ПОЗЖЕ, на **БИЛДЕ** (`create_set_feed_builders.py:917`),
    из тела джобы — там `feed_confirmed` есть, фолбэк срабатывает, кампании создаются.
  * Отсюда «половинчатый» симптом: tp5 живёт, tp7 нет — и врущий warning «tp7/tp5 не будут созданы».
- **Две дыры-триггера:**
  1. **QA-драйвер** слал `feed_confirmed` только в `/create_set`, а в `/set_plan` — ничего.
     Слать признак в `/create_set` **поздно**: план уже посчитан без product.
  2. **UI, кнопка «Создать всё равно»** (`dec==='confirmed'`) план НЕ пересчитывала — ставила
     `_FEED_CONFIRMED=true` и шла с уже посчитанным (без product) планом. Кнопка «Продолжить
     с другим фидом» (`dec==='fallback'`) план пересчитывает → tp7 появляется. При ОДНОМ И ТОМ ЖЕ
     аккаунте результат зависел от нажатой кнопки. Обе кнопки при этом давали идентичный план —
     `confirmed` был де-факто дублем «Создать без них».
- Решение (2026-07-19):
  * `slepok_qa_run.py` — в `body_plan` добавлен `"single_feed_fallback": True`: автопрогоны
    применяют фолбэк ПО УМОЛЧАНИЮ (у поп-апа никого нет). Тело `/create_set` не менялось.
  * `automation.js` (оба call-site: `createSet` + `createDraftsFromSlepok`) — `dec==='confirmed'`
    ставит `_SF_FALLBACK=true` и **пересчитывает план**, как `'fallback'`, но только когда
    `feed_alert.fallback_feed.id` реально есть (фолбэка нет → пересчитывать нечем, поведение прежнее).
    Подпись кнопки → «Создать всё равно (на фолбэк-фиде)» + title с именем фида.
  * `create_set_plan.py:513` — warning больше не врёт про tp5: «tp7 не попадут в план
    (tp5/tp3 достроятся на фолбэк-фиде)».
- Доказательство (LXC101, прод-венв, `/set_plan` как его зовёт QA-драйвер, `porg-ozge4ntu`,
  pavlov / «С пробегом», read-only): **БЫЛО** `total=20 {tp1_rsy:11, search_test:6,
  search_gallery:1, master:2}`, `feeds=0`, product НЕТ. **СТАЛО** `total=44` — те же
  `{tp1_rsy:11, search_test:6, search_gallery:1, master:2}` + **`product:24`**, `feeds=1`,
  `feed_id=3501091 (yandex-catalog-model-design-custom-name)`. 24 = 12 позиций tp7 структуры
  `pavlov` «С пробегом» × 2 (cpc/cpa). Остальные типы не изменились ни на единицу.
- Детект-запрос (read-only, прогнан 2026-07-19 — отдаёт `product=24 feeds=1`). ⚠️ Через HTTP
  снаружи НЕ работает: `/set_plan` за авторизацией, голый `urllib` даёт **401**. Только
  `app.test_client()` с проставленной сессией, как в QA-драйвере. Позиция tp7 помечена
  `type=='product'` (НЕ `kind`):
  ```bash
  ssh proxmox-ts "pct exec 101 -- bash -lc 'cd /opt/scripts/home/seoadvanced && /root/venv/bin/python3 - <<PY
  import sys; sys.path.insert(0,\"/opt/scripts/home/seoadvanced\")
  from direct.slepok_qa_run import VARIANTS, TP_SQ
  from direct.main import app
  with app.test_client() as cli:
      with cli.session_transaction() as s:
          s[\"logged_in\"]=True; s[\"is_admin\"]=True; s[\"username\"]=\"detect\"
      body={\"login\":\"porg-ozge4ntu\",\"agent\":\"pavlov\",\"site_type\":\"С пробегом\",
            \"variants\":VARIANTS,\"tp_sq\":TP_SQ,\"single_feed\":True,\"single_feed_fallback\":True}
      j=cli.post(\"/direct/api/set_plan\", json=body).get_json()
      n=sum(1 for i in j[\"plan\"] if i.get(\"type\")==\"product\")
      print(\"product=\",n,\"feeds=\",len(j.get(\"feeds\") or []))
      print(\"DEFECT\" if n==0 else \"OK\")
  PY'"
  ```
  `product=0` при непустой tp7 в структуре = дефект. Норма pavlov/«С пробегом» — 24.
- Статус: 🟡 фикс на Mac+LXC101 (md5 3 файлов совпали), план доказан фактом; **живой прогон
  СОЗДАНИЯ tp7 ещё не гонялся** (на аккаунте шла чужая джоба) — ждёт прогона.
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: **не** чинить это
  добавлением фолбэка в билдер tp7 — фид tp7 нужен РАНЬШЕ, на этапе плана, иначе item'ов просто
  нет и билдеру нечего строить. Признак обязан ехать в `/set_plan`, а не только в `/create_set`.
  ⚠️ Статусы фидов («Есть проблемы» / «Не найдено валидных») читать НЕ надо — решение Семёна
  2026-07-19: игнорировать статус верно, это не дефект.

### BUILD_COUNTS_SENT_NOT_UNIQUE — билдер считает ОТПРАВЛЕННЫЕ фразы, Директ хранит УНИКАЛЬНЫЕ (2026-07-19)
- Симптом: стабильный «недобор» ключей в сверке build⇄кабинет у 10+ кампаний сразу, разброс 4.7–18%
  (прогон `072884b4404b`: Поиск-Марка 1111→1021, РСЯ-Автосалон 183→150, РСЯ-Кредит 311→300 …).
  В отложенной фазе severity=`error` → вердикт прогона `fail` (14 из 15 ошибок) + `repair_plan`
  планировал **14 действий `keywords_repair`** — дозаливку в ИСПРАВНЫЕ кампании.
  ⚠️ Это НЕ потеря и НЕ лаг индексации: ре-прогон давал побайтово те же числа.
- Где: `create_set_tp1_builders` (v5 `keywords.add`), `create_set_text_builders` + `grid_create`
  (Grid `addKeywords`) → `build["keywords"]` → `grid_content_verifier._verify_build_vs_live`.
- **Root-cause — правило схлопывания Директа (выведено ЖИВЫМ зондом, porg-ozge4ntu, 2026-07-19):**
  Директ считает фразы одинаковыми и **схлопывает** их, возвращая на дубль id/Id **УЖЕ существующей**
  фразы (v5 — плюс `Warning 10140 «Ключевое слово уже существует»`; Grid `addedItems` — просто тот же
  `keywordId`, без всякого признака). Поэтому «сколько строк вернулось» = сколько ОТПРАВИЛИ.
  * **Схлопываются:** порядок слов (`дилер geely` ≡ `geely дилер`) · регистр · лишние пробелы ·
    оператор `+` (`+купить X недорого` ≡ `купить X недорого`) · словоформа (`geely дилеры` ≡
    `geely дилер`; `автосалон автомобили` ≡ `автосалон автомобиль`) · стоп-слова
    (`авторынок в адрес` ≡ `авторынок адрес`; `автосалон цены модельный ряд` ≡
    `автосалон модельный ряд и цены`).
  * **НЕ схлопываются (остаются отдельными фразами):** `!` (фиксация формы) · кавычки `"…"` ·
    скобки `[…]` · минус-слово в конце (`купить X недорого -отзывы`).
  * `,` и прочая пунктуация — ошибка `5002 «Используются недопустимые символы»`, Id не выдаётся.
  ⚠️ `_kw_clean` дедуплит только по точному lowercase — весь остальной класс дублей проходил насквозь.
- Решение (2026-07-19, коммит `80fddfc`): считать **уникальные id**, а не отправленные строки.
  Нормализацию НЕ переизобретаем — источник истины сам Директ, он вернул id.
  * `grid_create.unique_keyword_ids(added_items)` (новая функция) — число разных `keywordId`;
    применена в `grid_create.py` (create_full + add_text_content_to_existing) и
    `create_set_text_builders.py`. Fail-safe: строки есть, а `keywordId` ни у одной (смена
    Grid-схемы) → откат на `len(rows)`, чтобы не выдать ложное «0 из N создано».
  * `create_set_tp1_builders._v5_added_keyword_ids(chunk, AddResults, skip)` (новая функция) —
    множество `Id`, **сквозное по чанкам** (дубль может приехать другим чанком и вернуть прежний Id);
    `rep["keywords"] += len(_kw_ids)`.
  * Верификатор НЕ трогали → **0 новых запросов к Direct API/Grid**; калибровка `f2be7be`
    (спецключ `---autotargeting` не в счётчике) сохранена и покрыта юнит-кейсом.
- Доказательство на РЕАЛЬНЫХ данных (зонд-масштаб, pavlov ct0013, 200 фраз пака в свежую группу):
  отправлено 200 → старый счётчик 200, **новый счётчик 175, в кабинете ровно 175**
  (old_gap 25 → **new_gap 0**), 22 схлопнутые группы фраз. Черновая группа удалена после зонда.
- Статус: 🟡 фикс на Mac+LXC101 (md5 3 файлов совпали), ждёт живого прогона создания.
  Офлайн: py_compile Mac + прод-венв 3.11.2, **16 юнит-кейсов зелёные на прод-венве** (дубли →
  недобора нет; реальная потеря 150→120 → `BUILD_LIVE_UNDERCOUNT` + ремонт; `keywords_read=false`
  и `keywords_truncated` → молчит; спецключ не считается; группы/объявления не задеты).
  ⚠️ Пересчёт «14 ошибок / 14 ремонтов → 0» на самом прогоне `072884b4404b` **не верифицирован**:
  джоба удалена из `direct_automation_jobs` (в таблице остались только copy-джобы).
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: не писать собственную
  нормализацию фраз (порядок/словоформы/стоп-слова) — правило Директа шире любой локальной эвристики,
  а id из ответа даёт точный ответ бесплатно. Также не глушить `BUILD_LIVE_UNDERCOUNT` целиком —
  реальная потеря должна остаться видимой.

### RSYA_FINALIZE_TRANSIENT_SILENT_OK — транзиент финализации → кампания без бюджета, рапорт успеха (2026-07-19)
- Симптом: кампания создана и отрапортована как полностью успешная (`ok=true`, `failed=0`,
  `errors_log=NULL`), но в кабинете у неё **недельный бюджет 0** и НЕ привязаны уточнения/набор
  быстрых ссылок. Live: job `b0d25ad114c5` (`porg-ozge4ntu`, pavlov, «С пробегом», 20/20 создано),
  кампания 712885317: `rsya_finalized: false` +
  `finalize_warn: "РСЯ-finalize: [{"message": "Внутренняя ошибка сервера … reqId = 6517220183384299118"…`.
- Где: `create_set_finalize._finalize_rsya` (POST `UpdateCampaigns`) ← вызовы
  `create_set_tp1_builders.py:1355` (token-путь) и `:2301` (cookie-путь).
- Root-cause (ДВА независимых, оба подтверждены кодом+данными):
  1. **Ретрая не было вообще.** `GridClient._post` СОЗНАТЕЛЬНО без транспортного ретрая (комментарий
     на месте: `add_*` не идемпотентны, повтор = дубль). Под этот запрет попал и `UpdateCampaigns`,
     который идемпотентен по id → единичный 500 Яндекса убивал финализацию насовсем.
  2. **Недельный бюджет ставит НЕ `AddCampaigns`, а финализация** (`strategyData.sum`): падение
     финализации = кампания с `budget.sum=0`. Плюс тем же `UpdateCampaigns` привязываются
     `inheritableCallouts` / `inheritableSitelinkSet` / промо.
  3. **Сигнал глушился:** ошибка оседала в `result_d["finalize_warn"]`, позиция помечалась `ok=true`.
     `local_result_verifier` проверял `search_finalized` и `shopping_finalized`, а `rsya_finalized`
     — **не проверял вовсе**. Дефект был виден лишь КОСВЕННО (`WEEKLY_BUDGET_MISSING_LIVE`), и то
     только когда доезжало live-чтение спецификации.
- Решение (2026-07-19):
  * `grid_finalize.py` — `_GRID_TRANSIENT_MARKERS` (русские формулировки Директа, вкл. «внутренняя
    ошибка сервера») + `_grid_errors_transient()` + метод `GridClient.post_idempotent()`: ретрай
    3 попытки, backoff 2с/5с, на HTTP 5xx / транспортный сбой / ответ 200 с транзиентной ошибкой.
    ⛔ Метод отдельный НАМЕРЕННО — внутрь `_post` ретрай класть нельзя, иначе под него попадут
    `add_*` (журнал `RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD`). Применён в двух точках `UpdateCampaigns`:
    `GridClient.finalize` и `create_set_finalize._finalize_rsya`. Ошибки ВАЛИДАЦИИ маркеров не
    содержат → не ретраятся, поведение прежнее.
  * `local_result_verifier.py` — новый код **`RSYA_NOT_FINALIZED`** (severity **error**, report-only,
    с `id` и текстом причины). Ловит обе формы: `rsya_finalized is False` (token-путь) и
    `rsya_finalized={"error":…}` (cookie-путь). Report-only сознательно: кампания СОЗДАНА, ей не
    хватает докрутки — это случай для добивки, а не для удаления/пересоздания.
- Статус: 🟡 фикс на Mac+LXC101, ждёт живого прогона. Офлайн-доказательства: py_compile Mac +
  прод-венв LXC101 3.11.2 (md5 всех 3 файлов совпали), **24/24 юнит-кейса зелёные на прод-венве**
  (ретрай транзиента/500/обрыва, НЕ-ретрай валидации, «успех = ровно 1 вызов» = нет регресса по
  числу запросов, tri-state, регресс `SEARCH_NOT_FINALIZED`/`SHOPPING_NOT_FINALIZED`/
  `GRID_FINALIZE_WARN`/`BUILD_ERROR`/`NAME_HAS_NULL_TOKEN`). E2E на РЕАЛЬНОМ payload прогона
  `b0d25ad114c5` через `verify_live_create_set` **без единого live-снимка**: было `status=pass`,
  стало `status=fail`, `errors=1`, помечена ровно 1 кампания из 20 — именно 712885317, с reqId
  в сообщении. Ложняка на 19 успешных нет.
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: ретрай НЕ переносить
  внутрь `GridClient._post` — там он запрещён для `add_*` (дубли), см.
  `RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD`.
- ⚠️ Остаток (НЕ закрыто, нужна диагностика на СЛЕДУЮЩЕМ прогоне — данные того прогона утрачены,
  кампании 7128853xx удалены как черновики, а live-`counts` в джобе НЕ сохраняются):
  1. **Почему молчали `CALLOUTS_MISSING_LIVE` / `SITELINK_SET_MISSING_LIVE`.** Установлено: для
     712885317 live-чтение ДОЕХАЛО (`WEEKLY_BUDGET_MISSING_LIVE` идёт из `_verify_campaign_spec`,
     т.е. `campaign_spec_read=True`), а ассет-поля лежат в ТОМ ЖЕ ответе `CampaignsEditData`.
     Нормализация корректна (проверено live на 712885354/712885361: `campaign_assets_read=true`,
     `callout_ids`=8, `sitelink_set_id='1494693521'`). Ведущая гипотеза: частичная GraphQL-ошибка
     выбила ключи `inheritableCallouts`/`inheritableSitelinkSet`/`promoExtension` из row →
     `campaign_assets_read=False` → tri-state fail-safe отработал ПО ЗАДУМКЕ (молчание). Не доказано.
     ⚠️ Заявка «поля физически пусты» опиралась на `callouts_set`/`sitelink_set_id` из результата
     джобы — это **BUILD-side** ключи, они пишутся только в успешной ветке finalize и о live НЕ
     говорят ничего.
  2. **Ложный `NO_KEYWORDS_LIVE` (712885310: `search_zero_kw_groups=3` при `keywords_count=232`).**
     Установлено арифметически: оба счётчика пишутся в ОДНОМ месте (`grid_read.py:357,362`) из
     ОДНОГО списка `group_rows` (`kw_total += grp_kw`, `zero_kw += 1 если grp_kw==0`). При 3 группах
     20/71/141 обязано быть `zero_kw=0`. Значит у этой кампании в `group_rows` было **больше 3
     строк**, минимум 3 из них с нулём (дубли строк ЛИБО реально лишние пустые группы в кабинете).
     ⚠️ Предложенный гейт «`zero_kw < число разобранных групп`» НЕ применял: при 6 строках он даёт
     `3 < 6` → ложный ремонт останется, т.е. правка притворилась бы фиксом. Нужно сперва замерить
     `len(group_rows)` и `counts["adgroups"]` для такой кампании.
  3. **Системная причина, почему 1 и 2 недоказуемы задним числом:** live-`counts` нигде не
     сохраняются, а черновики между прогонами чистятся. Рекомендация: класть компактный срез
     `counts` (или хотя бы `groups_seen`/`adgroups`/`campaign_assets_read` + присутствовавшие ключи
     ассетов) в `live_verification` отчёта джобы — тогда следующий инцидент разбирается по факту.

### IMAGES_TAB_DUAL_WRITE_TP6 — замена картинки в МК шла ДВУМЯ транспортами сразу (2026-07-19)
- Симптом (по коду, живьём не проявлялся — путь ни разу не гоняли): одна замена картинки в tp6/tp7
  давала Grid `UpdateAdaptiveTextAds` **плюс** UAC full-PATCH (REPLACE-семантика) в ту же кампанию,
  плюс два аплоада одного файла в две библиотеки. Плюс порядок-зависимый ТИХИЙ пропуск: UAC-лег
  перечитывает `contents` ПОСЛЕ Grid-лега и мапит по СТАРОМУ `content_id` → `continue` без ошибки,
  `replaced` не растёт, задание выглядит успешным.
- Где: `content_images_routes.py::run_image_replace` (`need_rsya`/`need_uac` считались независимо,
  выполнялись ОБА блока) + `_replace_uac_images` (молчаливый `continue`).
- Root-cause: МК видна обоим инвентарям — её адаптивные объявления приходят в Grid-индексе, а те же
  картинки лежат в `contents` UAC-кампании (`_merge_inventory` их схлопывает по хэшу, на gcegsszl
  33 РСЯ + 42 UAC → 54 карточки, т.е. 21 общий хэш). Владельца никто не выбирал.
- Решение (2026-07-19, раунд 2 — актуальное): ОДИН транспорт на кампанию, владение = **факт чтения
  кампании UAC-инвентарём** и непустые `contents` (`_uac_owned_cids(uac_contents)`). Такие кампании
  исключаются из Grid-лега (`_replace_rsya_images(skip_cids=…)`, счётчик `ads_left_to_uac`). Аплоад в
  Grid-библиотеку идёт, только если у пары осталась работа вне UAC-кампаний → двойной аплоад устранён.
  Молчаливый пропуск закрыт: `continue` в `_replace_uac_images` → явная ошибка «старый креатив уже не
  в кампании»; покрытие адаптивов МК подтверждается чтением (`_verify_uac_mirror` → `mirror_check.stale`
  + строка в `errors`); **сверка легов** `legs_reconcile` — кампании, отданные Grid'ом UAC-легу, но
  UAC не записанные, попадают в `errors` (смешанный случай раньше выглядел успехом при пустом `errors`).
  Падение UAC-инвентаря запрет НЕ расширяет: `uac_contents` пуст → Grid-лег работает как раньше, а
  ошибка чтения уже лежит в `errors` задания.
- ❌ НЕ помогло / **внесло регресс** (первая версия того же фикса, 2026-07-19): `uac_owned` строился как
  «прочитанные UAC ∪ tp6/tp7 **по имени**». UAC `list_campaigns` — ПОДМНОЖЕСТВО tp6/tp7-по-имени
  (архивные МК + tp7-товарка, которые UAC-транспортом не пишут вовсе), поэтому объединение не
  страховало падение инвентаря, а систематически запрещало Grid-лег там, где UAC заменить НЕ МОЖЕТ.
  Live-замер `porg-gcegsszl`: `uac_owned` 60 кампаний (25 — только по имени), **12 хэшей оставались
  без единого транспорта**, 25 owned-кампаний вообще без `contents`. Ломалась в т.ч. кампания
  **704589546** — единственный живьём подтверждённый Grid-путь (`replaced:1, errors:[]`).
- ⚠️ **Граница выборки прошлого замера** (уводил в ложную сторону): «grid_only = 0 по 34 сравнённым
  кампаниям» верно ТОЛЬКО внутри 35 кампаний UAC-инвентаря. На множестве, к которому реально
  применялся запрет (`uac_owned` = 60), **grid_only = 125**. Мерить надо ровно то множество, к
  которому применяется запрет.
- Живой read-only probe 2026-07-19 (мутаций 0), ДО(union) → ПОСЛЕ(только UAC):
  `porg-gcegsszl` owned 60→**34**, grid_only 125→**0**, потеря ключей 12→**0**, owned без contents
  25→**0**; 704589546 owned ДО=True → ПОСЛЕ=False (снова Grid-путь). `porg-pvrbl7mh` без изменений
  (owned 10→10, grid_only 0, потеря 0) — дефект был account-specific, «на одном аккаунте чисто»
  доказательством НЕ является. Покрытие инвентаря не тронуто: gcegsszl 41 адаптив / 33 картинки,
  pvrbl7mh 2722 / 315.
- Статус: 🟡 ждёт живого прогона комбинированного пути (мутация). Регресс-тест в репозитории:
  `direct/tests/test_content_images_transport_split.py` (8 кейсов, в т.ч. «tp6-по-имени вне UAC →
  Grid-путь» и смешанный случай → громкая ошибка).
- ⚠️ Остаток: «адаптив МК — проекция `contents`» доказано СТРУКТУРНО (grid_only=0 внутри UAC-кампаний),
  а не мутацией. Именно поэтому проверка зеркала оставлена в рантайме. `stale` может означать и
  задержку индексации Директа (текст ошибки это называет) — на первом живом прогоне сверить.

### IMAGES_TAB_V5_CAMPAIGN_LIST_BLIND — вкладка «Смена изображения» не видела адаптивы tp6/tp7/tp8 (2026-07-19)
- Симптом: тихая неполнота. На `porg-gcegsszl` вкладка показывала **0 картинок РСЯ**, хотя на аккаунте
  41 адаптивное объявление; ошибок UI не показывал. На `porg-pvrbl7mh` дыра не проявлялась — там
  адаптивы лежат в v5-видимых `TEXT_CAMPAIGN`, и прошлая проверка сочла покрытие полным.
- Где: `content_images_routes.py::_rsya_inventory` (список кампаний из v5 `campaigns.get`, список
  объявлений из v5 `ads.get`).
- Root-cause: v5 `campaigns.get` НЕ отдаёт tp6/tp7 (UAC/МК/товарка) и tp8 (Telegram). Замер
  2026-07-19 `porg-gcegsszl`: v5 — 82 кампании, Grid — 157 (неархивных 69; только-Grid неархивных 42:
  35 × tp6/tp7 + 7 × `tp8_…/GdPostCampaign`). ВСЕ адаптивы аккаунта лежат в v5-невидимых кампаниях →
  `work_cids` их не содержал → `adaptive_ads_for_update` по ним не звался. Второй слой: даже с верным
  списком кампаний v5 `ads.get` по ним объявлений не отдаёт — id объявлений тоже надо брать из Grid.
- Решение (2026-07-19): источник и кампаний, и объявлений — Grid по куке (0 баллов v5).
  `_grid_campaigns` (полный список, включая архивные — v5 их отдавал, срезать = поменять одну слепую
  зону на другую) + `_grid_ads_index` (лёгкий `id/campaignId/__typename` через уже принятый
  `_ads_rows_paginated`). v5 `campaigns.get` оставлен КРОСС-ЧЕКОМ: кампания видна v5, но не пришла из
  Grid → строка в `skipped`. Не адаптивные типы объявлений перечисляются в `skipped` поимённо с числами.
- Статус: ✅ подтверждено live read-only probe (мутаций 0). `porg-gcegsszl`: адаптивов 0 → **41**
  (37 с картинками), уникальных картинок РСЯ 0 → 33. `porg-pvrbl7mh`: 2718 → **2722**, картинок
  312 → 315 (ничего не потеряно). Цена: gcegsszl 27.3с → 47.5с (+20с — индексируются ещё 88 архивных
  кампаний), pvrbl7mh 55.2с → **49.5с** (быстрее: тяжёлый RMW-проход теперь только по кампаниям с
  адаптивами, а не по всем).
- НЕ помогло ранее: — (первая правка этого класса). Родственная уже закрытая дыра того же типа
  «тихая неполнота инвентаря» — одностраничное чтение Grid (limit 5000), починено `_ads_rows_paginated`.
- ⚠️ Остаток: публичного «списка всех кампаний» в `grid_finalize.GridClient` нет, селект живёт в
  `content_images_routes.py`. Правильное место — метод `GridClient.list_campaigns_basic()`.

### IMAGES_TAB_SEARCH_TP_BLIND — вкладка «Смена изображения» отсекала поиск tp2/tp4 целиком (2026-07-19)
- Симптом: в `skipped` строка «поиск tp2/tp4 — картинки не поддерживаются — 11» (`porg-bzti5ud7`).
  Поисковые кампании не читались вовсе, т.е. картинку, которая там ФАКТИЧЕСКИ есть, заменить было нельзя,
  и не было видно, сколько именно объявлений не покрыто.
- Где: `content_images_routes.py::_rsya_inventory` — константы `_SEARCH_TPS`/`_SEARCH_SKIP_REASON`
  и фильтр `work_cids = [cid for cid in camp if cid not in set(search_cids)]`.
- Root-cause: отсечение по tp-МЕТКЕ ИМЕНИ вместо факта наличия картинок. По спеке в поиске картинок
  быть не должно (см. `IMAGES_FORBIDDEN`), но спека — не гарантия состояния кабинета; предположение
  подменяло чтение. Тот же класс, что `IMAGES_TAB_V5_CAMPAIGN_LIST_BLIND`: тихая неполнота инвентаря.
- Решение (2026-07-19): tp2/tp4 читаются наравне со всеми (`work_cids = list(camp)`), константы удалены.
  Есть непустой `imageHashes` → карточка в инвентаре. Нет картинок → кампания просто не даёт карточек,
  отдельной строки в `skipped` нет. В `skipped` остаётся ТОЛЬКО реально непокрытое — неадаптивные типы
  объявлений (`GdTextAd`/`GdShoppingAd`/`GdListingAd`/`GdMlAutoSuggestAd`), поимённо с числами.
- Статус: 🟡 ждёт прогона UI. Live read-only probe (мутаций 0) подтвердил отсутствие регресса и рост
  честности `skipped`, но САМ сценарий «заменили картинку в поиске» НЕ проверен: на трёх прогнанных
  аккаунтах адаптивных поисковых объявлений с картинками нет (`cards_touching_tp2_tp4 = 0`),
  поисковые несут `GdTextAd`. Картинки РСЯ/UAC не потеряны: `porg-bzti5ud7` 5→5, `porg-gcegsszl`
  33→33 (merged 54→54), `porg-pvrbl7mh` 315→315, сверка по ХЭШАМ, `LOST_cards` пуст на всех трёх.
- ⚠️ Встречный механизм: `repair_media.execute_images_forbidden_repair` чистит `imageHashes=[]` у поиска.
  Риск цикла «заменили → репейр стёр» РЕАЛЕН, но узок: авто-путь (`repair_auto`) исполняет его только
  из плана `_create_set_live_verification`, а тот `IMAGES_FORBIDDEN` не эмитит вовсе; единственный
  эмитент — `campaign_spec_audit._audit_search_images`, и его авто-обёртка `_run_spec_audit_and_fix`
  images_forbidden НЕ чинит (только CLI `python3 -m direct.campaign_spec_audit --fix`). Т.е. снести
  замену может только ручной CLI-фикс или будущее подключение этого репейра в авто-цикл.
- НЕ помогло ранее: — (первая правка этой сигнатуры).

### IMG_ACCOUNT_MAP_REREAD_QUADRATIC — картинки перезаливались каждый прогон + чтение аккаунта на КАЖДОЙ РК (2026-07-19)
- Симптом: не ошибка создания, а ПЕРФ — предзагрузка картинок 134 с/прогон (829 файлов, 14 потоков,
  16% фазы create-set); по ходу набора cookie-кампании создавались всё медленнее.
- Где: `create_set_tp1_builders.py:_grid_account_image_hashes` (сырое чтение), вызовы —
  `_preupload_tp1_images` (прогрев), `_create_tp1_via_cookie` (tp1), `create_set_feed_builders.py`
  `_create_text_via_cookie` (tp2/tp4).
- Root-cause (два, независимых): (1) сырое чтение отдаёт только картинки, ПРИВЯЗАННЫЕ К ОБЪЯВЛЕНИЯМ —
  после `delete_drafts` объявлений нет → `account_lib≈2`, хотя imageHash жив в БИБЛИОТЕКЕ логина;
  прогрев считал набор незалитым и лил все 829 заново. (2) читатель БЕЗ кэша звался на КАЖДОЙ
  cookie-кампании и перечитывал ВСЕ кампании+объявления аккаунта → квадратичный рост по ходу прогона.
- Решение (2026-07-19, коммит `df579e5`): процесс-глобальный кэш `{basename: imageHash}` по логину
  (`_account_image_map` / `_account_image_map_merge` / `_account_image_map_drop`, lock, TTL
  `DIRECT_ACC_IMG_MAP_TTL`=1800с). Карта **МЁРЖИТСЯ, не замещается** — это и чинит обнуление после
  delete_drafts. Свежезалитые хэши доливаются сразу → следующая РК набора берёт без сети. Чтение
  аккаунта: 1 на прогон (`force=True` только в прогреве) вместо 1 на кампанию.
- Статус: 🟡 ждёт живого прогона. Проверено пока только поведенческим тестом (мок сырого читателя):
  1 прогрев + 20 кампаний → 1 чтение вместо 21; после обнуления аккаунта старые хэши в карте живы.
- ⚠️ Осознанный остаток: imageHash, удалённый из кабинета ВРУЧНУЮ, живёт в кэше до TTL и даст отказ
  Директа на создании объявления. Автосброс по тексту ошибки НЕ реализован — сигнатура отказа
  «неизвестный imageHash» фактом не подтверждена, гадать не стал. Ручной сброс: `_account_image_map_drop(login)`.
- НЕ помогло ранее: — (первая правка этого класса). Смежное: `_GRID_IMG_HASH_CACHE` (login+realpath,
  `create_set_feeds.py:472`) существует давно и НЕ закрывал проблему — он переживает набор, но не
  рестарт воркера, и не влияет на `account_map`, по которому прогрев решает «лить или не лить».

### VERIFY_BUILD_LIVE_GAP — результат билдера не сверялся с кабинетом (ЭТАП 1 усиления проверок) (2026-07-18)
- Симптом: кампания создана, `build` рапортует N групп/объявлений/ключей, а в кабинете их МЕНЬШЕ —
  верификатор молчал. Сверка была только с НУЛЁМ (`local_result_verifier.py:16-28`,
  `NO_ADGROUPS_LIVE`/`NO_ADS_LIVE`), «создал 27 групп → в кабинете 9» проходило как «pass».
  Плюс: гео кампании, счётчик Метрики, цель, недельный бюджет, статус черновика, расписание показов
  и UTM-на-группах (DoD #2, числился «P1 не покрыт») не проверялись ВООБЩЕ.
- Где: `grid_content_verifier.verify_grid_content`, `live_verifier.verify_live_create_set`,
  `grid_read._enrich_group_targeting` / `_enrich_campaign_invariants`,
  `grid_finalize.read_campaign_invariants` / `groups_for_edit`, `campaign_spec_audit`.
- Root-cause: два нужных запроса УЖЕ выполнялись, но их результат частично выбрасывался.
  (1) `CampaignsEditData` извлекал ~12 полей из ~120 — `metrikaCounters` / `meaningfulGoals{goalId}` /
  `bannerHrefParams` / `hasAddMetrikaTagToUrl` / `strategy.budget{sum,period,autoProlongation}` /
  `status.primaryStatus` / `aggregatedStatusInfo.status` / `timeTarget{timeBoard,idTimeZone}` уже
  лежали во фрагменте `UnifiedCampaign`. (2) `GroupsForEditLite` нормализовал гео/минуса/ключи
  per-group, но `grid_read` писал флаги ТОЛЬКО для поисковых tp2/4/5 — группы tp1/tp3 читались тем же
  батч-запросом и выбрасывались; `regionsInfo.regionIds` и `trackingParams` выбрасывались у всех.
- Решение (2026-07-18, ЭТАП 1 — ТОЛЬКО детект, ремонт не реализован):
  * `grid_finalize.read_campaign_invariants` — +12 tri-state полей спецификации, флаг `campaign_spec_read`;
    `groups_for_edit(..., meta=)` отдаёт признаки ОБРЕЗКИ по `_GFE_LIMIT`.
  * `grid_read._enrich_group_targeting` — охват tp2/4/5 → tp1–tp5; +`campaign_region_ids`/
    `geo_missing_groups`/`geo_regions_inconsistent`/`utm_missing_groups`/`keywords_truncated`.
  * `grid_content_verifier` — `BUILD_LIVE_MISSING`/`BUILD_LIVE_UNDERCOUNT`, `GEO_MISSING_LIVE`,
    `GEO_INCONSISTENT_LIVE`, `UTM_MISSING_LIVE`, `METRIKA_COUNTER_MISSING_LIVE`,
    `CAMPAIGN_GOAL_MISSING_LIVE`, `METRIKA_TAG_OFF_LIVE`, `WEEKLY_BUDGET_MISSING_LIVE`,
    `BUDGET_PERIOD_UNEXPECTED_LIVE`, `CAMPAIGN_NOT_DRAFT_LIVE`, `TIME_TARGET_MISSING_LIVE`
    (каталог — DOD.md §1.b).
  * Фазы: in-job недобор = `warn` (dcr-демон доливает контент ПОСЛЕ статуса done, in-job проверка его
    структурно не видит), отложенная фаза = `error` + repair.
- ⚠️ Три ловушки, снятые ЯВНО (иначе гарантированные ложняки):
  (1) **лимит 10000** (`_GFE_LIMIT`/`_SC_LIMIT`, пагинация за предел не работает) — ответ ровно на лимит
  помечается `keywords_truncated=True`, и по КЛЮЧАМ такая кампания не судится вовсе;
  (2) **tp1/tp3 автотаргет-группы** законно живут БЕЗ реальных ключей (спецключ `---autotargeting`
  оседает как `relevanceMatch`, `create_set_tp1_builders.py:297-304`) → активный `relevanceMatch`
  гасит zero-kw для tp1/tp3; `WRONG_AUTOTARGET` для tp1 не выдаётся (у РСЯ автотаргет широкий by design);
  (3) **товарка** (`build.ads=0`, `shopping_ads>0`) — сверка идёт по СУММЕ `ads+shopping_ads+listing_ads`
  против общего live-счётчика `ads`, и только в сторону НЕДОБОРА (перебор — норма, Grid считает все типы).
- 0 новых обращений к API — ЗАМЕРЕНО (baseline `git archive HEAD` vs текущий, тот же набор кампаний):
  **12 Grid-операций до = 12 после**, тот же per-op разрез. `_show_condition_kw_counts` (запрос НА
  КАМПАНИЮ) намеренно оставлен только для tp2/4/5 — иначе охват tp1/tp3 стоил бы +1 запрос на РСЯ-кампанию.
- Статус: 🟡 ждёт живого прогона. Офлайн: py_compile (Mac + прод-венв LXC101, md5 совпали),
  70 юнит-кейсов зелёные на обоих (tri-state / расхождение / UAC / товарка / обрезка / регресс
  `NO_IMAGES_LIVE`/`NO_ADPRICE_LIVE`/`CALLOUTS_MISSING_LIVE`/`SITELINK_SET_MISSING_LIVE`/`PROMO_MISSING`/
  инвариант-галочки/`WRONG_AUTOTARGET` tp2).
- **Доработка по ревью (2026-07-19) — отложенная сверка была МЕРТВА, теперь включена:**
  * **A1 (главное).** `automation_runtime._run_spec_audit_and_fix` собирал `job_result` БЕЗ ключа
    `results` → `campaign_spec_audit.audit_account_jobs` получал пустой `builds_by_cid` →
    `_audit_build_vs_live` молчал во ВСЁМ отложенном проходе. Заявленный «error в delayed» не
    существовал, работал только in-job `warn` — половина функциональности. Фикс: `job_result`
    несёт `"results": ctx.get("results") or []` (значение уже лежит в ctx — тот же `results_tree`,
    которым делейд-цикл кормит live-верификацию).
  * **A1-б.** Вторая половина той же дыры: `phase="delayed"` в live-путь НИКТО не передавал —
    дефолт `in_job` глушил `BUILD_LIVE_UNDERCOUNT` до `warn` даже в dcr-проходе. Фикс:
    `queue_server._live_plan` (:1055) зовёт `_create_set_live_verification(..., phase="delayed")`.
    ⚠️ `create_set_repairing._attach_post_repair_verification` НАМЕРЕННО оставлен на `in_job`:
    он бежит сразу после мутации ремонта, контент ещё не осел.
  * **A2.** `CAMPAIGN_NOT_DRAFT_LIVE` флагал «всё, что не DRAFT». Допущение о значении статуса
    Семён подтвердил живым Grid-запросом (кампания 712882029: `primaryStatus='DRAFT'`,
    `aggregatedStatus='DRAFT'`), но гейт сделан устойчивым к смене словаря Grid: флагаем только
    при попадании в ЯВНЫЙ перечень `_NON_DRAFT_STATUSES` (ACTIVE/ACCEPTED/ENDED/STOPPED/SUSPENDED/
    PAUSED/MODERATION/WAIT_MODERATING/REJECTED), незнакомое значение → МОЛЧИМ. `ARCHIVED` не
    включён (архивный черновик — не «опубликована»).
  * **A3 (охват).** `trackingParams` живёт во фрагменте `...on GdUnifiedAdGroup`, поэтому у групп
    другого типа не приходит. Разбор показал: это НЕ отдельная дыра UTM — `_enrich_group_targeting`
    отсеивает не-`GdUnifiedAdGroup` ещё раньше (`grid_read.py`, гейт `supported`), т.е. ВСЕ
    пер-групповые проверки (zero-kw / автотаргет / гео / UTM) покрывают ровно один и тот же
    набор групп. Расширять GraphQL-запрос фрагментом на угаданный typename НЕ стали: схему для
    других типов офлайн не проверить, а невалидное поле уронило бы ВЕСЬ `GroupsForEditLite`
    (это и RMW-докрутка). Вместо этого охват сделан ИЗМЕРИМЫМ: счётчик `groups_unsupported`
    (0 доп. запросов) + `GROUPS_TYPE_UNSUPPORTED_LIVE` (warn, report-only). Пока сервис создаёт
    ЕПК-группы код не появляется никогда; появился — значит часть кампании не проверяется, и мы
    это УВИДИМ, а не пропустим молча.
  * **A4 (обрезка по группам).** `counts["adgroups"]`/`counts["ads"]` берутся НЕ из
    `GroupsForEditLite`, а из отдельных запросов `AdGroups`/`Ads` с `limit 5000` — их обрезка
    никак не отслеживалась. Заведены `adgroups_truncated`/`ads_truncated` (ответ ровно на лимит),
    оба измерения при True не судятся. `_meta["adgroups_truncated"]` из `GroupsForEditLite`
    ИЛИ-ится сверху. То же в отложенном `_audit_build_vs_live`: измерение «группы» теперь
    загейчено (раньше гейт стоял только на ключах, хотя `len(supported)` из того же ответа).
  * **A5 (маскировка суммой).** Сверка объявлений идёт по сумме `ads+shopping_ads+listing_ads`, и
    перекос по типам сходится (build 10 текстовых + 100 товарных против факта 0 + 110 = 110).
    Раздельная сверка оказалась ДЕШЁВОЙ: `adaptive_total` — уже посчитанное число
    `GdAdaptiveTextAd` из существующего ответа `AdaptiveImages` (там и так разбирается
    `__typename`), 0 новых запросов. Заведён `BUILD_LIVE_TEXT_ADS_UNDERCOUNT` — **warn,
    report-only, без repair-кандидата**: соответствие «`build.ads` ⇒ `GdAdaptiveTextAd`» живым
    прогоном НЕ подтверждено, и если какой-то билдер кладёт плоский `GdTextAd`, код даст ложняк.
    Поднимать до error — только после чистого живого прогона.
- 0 новых обращений — ПЕРЕЗАМЕРЕНО после доработки (baseline `git archive HEAD` = `80bdf40` vs
  текущий, идентичный харнесс): **9 = 9 Grid-операций**, per-op разрез побайтово тот же, разница
  только в ДАННЫХ (новые tri-state поля заполнены).
- **КАЛИБРОВКА по ПЕРВОМУ живому прогону (2026-07-19, job `2b9d58d01e28`, `porg-ozge4ntu`, слепок
  pavlov, «С пробегом», 20/20 создано, 0 упавших).** Прогон дал 21 error-issue — при разборе
  **настоящих дефектов ноль, все 21 ложные**. Три калибровки:
  * **`METRIKA_TAG_OFF_LIVE` (18 из 20) — ПРОВЕРКА СНЯТА (решение Семёна).** Ложная ПО ПОСТРОЕНИЮ:
    билдер САМ ставит `hasAddMetrikaTagToUrl=False` (`grid_create_payloads.py:77`), и браузерный
    эталон `grid_uc_template.json:286` — тоже `false`. Флаг = авторазметка Яндекса (`yclid`), она
    НЕ пересекается с нашими UTM (те живут в `trackingParams` групп). Канон (DoD §3.0, инварианты
    #1/#2) авторазметки не требует нигде. Live: у всех проверенных `has_add_metrika_tag_to_url=false`
    при живом `metrikaCounters=["109986170"]` и `utm_missing_groups=0`. Код убран из
    `_verify_campaign_spec`, строка убрана из каталога DOD.md §1.b. **Билдер НЕ трогали — он прав.**
  * **`BUILD_LIVE_MISSING` по ключам (3) — ложное 100%, загейчено.** Все три автотаргетинговые:
    live v5 у 712883341 = ровно 1 ключ `---autotargeting`, у 712883264 = 9 штук, реальных ключей 0;
    Grid `showConditions{typeIn:[KEYWORD]}` их не видит (живут как `relevanceMatch`) → `expected` =
    число групп = число спецключей. Причина: гейт `relevanceMatch`/`at_by_design` применялся только
    к `NO_KEYWORDS_LIVE`, а `_verify_build_vs_live` о нём не знал. **Ложняк уже тянул лишние
    мутации: `repair_gate.keyword_repair_campaigns=3`, actionable.** Две правки:
    (а) спецключ больше НЕ попадает в `build["keywords"]` (`create_set_tp1_builders.py:313-322`,
    фильтр по `_AUTOTARGET_KW` позиционно по `AddResults`) → у чистого автотаргета `expected=0` и
    измерение не сверяется вовсе; (б) `grid_read` считает `at_by_design_kw_groups` (тот же признак,
    что гасит zero-kw), и `_verify_build_vs_live` при нём НЕ выдаёт `BUILD_LIVE_MISSING` по ключам.
    ⚠️ **Гейт узкий — намеренно:** гасится только ветка `live<=0`, `BUILD_LIVE_UNDERCOUNT` остаётся.
    Глушить недобор целиком нельзя: у СМЕШАННОЙ кампании (часть групп на автотаргете, часть с
    ключами) это скрыло бы реальную потерю ключей. После правки (а) цифры недобора и так честны.
  * **`BUILD_LIVE_UNDERCOUNT` (14) — severity ВЕРНОЕ, врал ТЕКСТ.** В БД у всех 14 `severity=warn`,
    `phase=in_job` — фаза доезжает, `under_sev` работает как задумано, в 21 error-issue они НЕ
    входят; механика НЕ тронута. Но note «отложенный демон ещё доливает контент» — **неправда**:
    спустя ~12 ч живые счётчики идентичны in-job (712883310: 460, 712883328: 232, 712883335: 425,
    712883355: 196). Реальная картина 712883310: live 469 = 460 реальных + 9 спецключей; build 492
    → 9 артефакт спецключа, **23 (~4.7%) реально не осели** (билдер считает `AddResults` с `Id`,
    Директ схлопывает дубли фраз позже). Note переписан честно: «часть ключей/объявлений может
    схлопнуться Директом как дубли; на in-job возможен лаг индексации Grid».
- Статус: 🟡 ждёт живого прогона (калибровка от 2026-07-19). Офлайн после калибровки: py_compile
  Mac + прод-венв LXC101 3.11.2 (md5 всех 4 файлов совпали), **14 юнит-кейсов зелёные на прод-венве**,
  включая end-to-end через `repair_planner.build_repair_plan` + `repair_gate.summarize_repair_gate`:
  `keyword_repair_campaigns` **3 → 0**. Регресс подтверждён: `NO_KEYWORDS_LIVE`, `WRONG_AUTOTARGET`,
  `BUILD_LIVE_UNDERCOUNT` (warn на in-job / error+ремонт на delayed), соседние spec-коды, tri-state
  fail-safe, гейт не задел измерения «группы»/«объявления». Эквивалентность zero_kw до/после
  реструктуризации `grid_read` доказана исчерпывающей таблицей истинности (40 комбинаций, 0 расхождений).
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: `METRIKA_TAG_OFF_LIVE`
  снят СОЗНАТЕЛЬНО (2026-07-19) — не заводить заново «разметку ссылок» как дефект; наш путь —
  свои UTM на группах БЕЗ авторазметки yclid.


### UI_STALE_SLEPKI_TREE — браузер рисует УСТАРЕВШЕЕ дерево слепков (304 на изменившуюся структуру) (2026-07-18)
- Симптом: правка слепка сделана и лежит на диске, но страница `/direct/automation/slepki` показывает
  старое дерево. Факт: в `slepki/dmp.json` 36 tp2-items уже с `aon_n000_ct019_ag001_g00`, а браузер
  запрашивал старый `aon_n000_ct001_ag011_g00`; оба `ui_structure` в логе — `304`.
- Где: `automation_runtime._ui_structure_payload` (:626), сигнатура кэша → `sig` → ETag.
- Root-cause: сигнатура строилась как `stat()` по списку имён с фильтром `if (_HERE/name).exists()`.
  После сплита монолита на per-slepok файлы (`direct/slepki/*.json` + `slepki_store.assemble()`,
  коммиты `9532844`/`55a953a`) файла `slepki_structure.json` на диске НЕТ → он **молча** выпал из
  сигнатуры (замер: в сигнатуре остались только `targeting_profile.json` и
  `gen_ses_source_manifest.json`). Правка ЛЮБОГО слепка не меняла ETag → `304` → старое дерево.
  ⚠️ Это возврат инцидента 2026-07-16 (tp5 kuderko с «Фидами» висел 8 часов): ETag-механизм тогда
  и завели, а сплит структуры его тихо обесточил — `exists()`-фильтр превратил пропажу источника
  в «нет проблем» вместо ошибки.
- Решение (2026-07-18): сигнатура берёт `slepki_store._signature()` (mtime+size `_order.json` + всех
  part-файлов — тот же ключ, по которому store инвалидирует свой `assemble()`-кэш) + по-прежнему
  `targeting_profile.json` и source-манифесты. `automation_runtime.py:623-640`.
- Проверено фактом (in-process на LXC101, `direct.main.app` test_client): неизменная структура →
  `304`; правка одной группы в `slepki/dmp.json` → ETag `086de61e…` → `413dcf3f…`, HTTP `200`,
  новая группа ВИДНА в ответе. До правки та же правка давала неизменный `sig` (`040c80c0…`).
- Статус: 🟡 фикс задеплоен (рестарт `direct-create` :5020 — именно он отдаёт `/direct/api/ui_structure`,
  см. nginx: страница на :5023, а `ui_structure`/`tags` падают в общий `/direct/` → :5020),
  ждёт подтверждения живым открытием страницы Семёном.
- НЕ помогло ранее: — (первая правка этой сигнатуры; сам ETag-механизм из инцидента 2026-07-16 верен,
  сломан был только состав сигнатуры).
- 🔒 Урок на будущее: при разбиении/переносе файла-источника — проверять, не входит ли он в
  кэш-сигнатуру/ETag. Фильтр `if exists()` в сигнатуре ОПАСЕН: пропажа источника становится
  «пустым местом» вместо падения.

### RETRY_DELIVERY_BURNS_UNITS — доставка недостающих позиций шла за баллы, а не по куке (2026-07-18)
- Симптом: после финала создания+добивки ретрай-джоба уходила `via_cookie=false` → жгла баллы v501.
  Факт с прода: родитель `194c27f8c9b5` → дочерняя `9f7be1ef7fb3` с `via_cookie=false`, доставка ~11 мин
  от завершения родителя.
- Где: `queue_server._requeue_missing_positions_once` (:591) — ставит create-джобу через `_job_new_web`.
  ⚠️ Это **НЕ** `repair_auto.queue_recreate_repair_job` (частая ошибка диагностики: та функция — про
  recreate-репейр и там «баллы первичны» :652-662 живёт осознанно; путь доставки позиций отдельный).
- Root-cause: `rbody` копировалось от родителя, фильтруясь только по префиксу `_`. `via_cookie` под
  фильтр не попадает → наследовался родительский `false` (штатное создание = units-first) → ретрай
  повторял units-транспорт. Явного решения о транспорте ретрая в коде НЕ было — это был наследуемый
  дефолт, а не выбор.
- Решение (2026-07-18, требование Семёна «ретрай по ошибкам — только по кукам через 1-2 мин»):
  `rbody["via_cookie"]=True` для джобы-доставки. Путь значения: `_job_new_web` → body →
  `create_set_input.py:154 bool(body.get("via_cookie"))` → `create_set_orchestrator.py:168`.
  **ИСКЛЮЧЕНИЕ — сегментный tp5** (новый предикат `_position_needs_units`, :634): кукой он физически
  НЕ создаётся (`_create_shopping_via_cookie` не принимает segment, лепит generic ct0000-группу на все
  сегменты — инцидент 2026-07-06, 5 одинаковых tp5 porg-psm5h7q6), поэтому cookie-путь для него
  захардкожен в явный `NO_BRAND_SEGMENTS_AVAILABLE` (`create_set_gallery.py:82-109`). Признак — тот же,
  что там: `tp5_segment` | `only_gks` | `only_cts`. Есть такие позиции → куку НЕ форсим (флаг
  set-уровневый, набор один), доставка идёт прежним транспортом + лог. tp5 «Фиды»/products_only
  сегмента не имеет → идёт кукой штатно.
- **Доработка-2 (2026-07-18, решения Семёна) — РАЗДЕЛЕНИЕ джоб доставки.** Первая редакция гнала
  ВЕСЬ набор прежним транспортом (за баллы), если в нём была хоть одна сегментная tp5 — флаг
  `via_cookie` set-уровневый (`create_set_input.py:154` → `orchestrator:168`, per-item транспорта в
  оркестраторе НЕТ). Теперь `_requeue_missing_positions_once` ставит **до двух** джоб:
  cookie-capable → `via_cookie=True` (0 баллов), сегментные tp5 → отдельная джоба на прежнем
  (унаследованном от родителя) транспорте. Пустой набор → лишняя джоба не создаётся.
  Учёт родителя мульти-child-безопасен ПО КОНСТРУКЦИИ: `_resume_children` — dict по `child_jid`,
  `_active_children` — список; родитель терминален только когда закрылись ОБЕ (`queue_server.py:788-865`).
  Гонки за аккаунт нет: `_CREATE_MAX_PER_AGENCY=1` + кросс-процессный `_agency_gate_claim`
  (UNIQUE по agency) → сёстры одного агентства идут строго по очереди. Дедупа они не боятся:
  `_job_new_web(..., dedup_login=False)`; гейт `_job_db_active_by_login` проверяется ОДИН раз до
  постановки обеих. Маркер `auto_requeue_missing` — `job_id` (первая) + новый `job_ids` (все);
  читателей маркера вне `queue_server.py` нет.
- Интервалы (решение Семёна 2026-07-18: **первая добивка через 3 мин, повтор через 5 мин**;
  прежние 60/60 из первой редакции ОТМЕНЕНЫ): `job_repository.py:19` = **180** (первый запуск —
  default `delay_seconds` в `_delayed_content_repair_save`, зовётся без аргумента из
  `queue_server.py:940`), `queue_server.py:68` = **300** (reschedule :744 + поле `run_after_seconds` :989).
  ⛔ Числа РАЗНЫЕ НАМЕРЕННО — не «унифицировать» обратно (в обоих местах стоит комментарий-стоп).
  Обоснование: контент оседает в кабинете 5-10+ мин (`STATE.md`), ранний проход чинит то, что и так
  привяжется, и выносит ложный вердикт «не починилось». Поллинг демона 60с → фактические окна:
  первый проход 180-240с (3-4 мин), повтор 300-360с (5-6 мин); суммарное окно при
  `MAX_RESCHEDULES=1` = 480-600с (8-10 мин) чистого ожидания + длительность самих проходов.
  Проходы/итерации/бюджеты НЕ трогали: `MAX_ITERATIONS=2`, `TIME_BUDGET=1200`, `MAX_RESCHEDULES=1`.
- **Доработка-3 по ревью (2026-07-19) — orphan при частичном сбое разбивки (Б1).** Маркер
  `auto_requeue_missing` ОДНОРАЗОВЫЙ (гейт `queue_server.py:577`). После разбивки на две джобы
  появился сценарий: cookie-джоба не создалась (`_job_new_web` вернул None → `continue`), units-джоба
  создалась — маркер всё равно ставился → следующая попытка навсегда заблокирована → cookie-позиции
  теряются. До разбивки такого не было: при `None` маркер не ставился и цикл повторял. Фикс: части
  считаются заранее (`_parts`), и если `len(new_jids) < len(_parts)` — маркер НЕ ставим, возвращаем
  `None` + лог. Следующий финал dcr повторит; уже созданные позиции к тому моменту живы в кабинете
  и из `missing` выпадут (сверка идёт по именам из `_grid_list_campaigns`).
- **Б2 (след в логе).** Функция возвращала `new_jids[0]`, поэтому startup-reconcile при рестарте
  воркера показывал ОДНУ джобу из двух. Возврат изменён на список; оба вызывающих обновлены
  (`:319` логирует все, `:1222` возврат игнорирует). Поведение не менялось.
- Не сломано: штатное создание не затронуто — `via_cookie` в `queue_server.py` присваивается только
  в джобе-доставке (эта правка), в куки-докрутке деферреда и при явном согласии юзера через попап 152;
  тело обычной create-джобы флага не получает.
- Статус: 🟡 код на Mac + доезжает Mutagen на LXC101 (md5 совпал), py_compile Mac+прод OK, pyflakes 0.
  **Живьём НЕ проверено** — нужен прогон с недобором СМЕШАННОГО состава: должны появиться ДВЕ дочерние
  джобы (cookie `via_cookie=true` + units), исполниться ПОСЛЕДОВАТЕЛЬНО, карточка родителя стать
  терминальной только после второй, баллы не двинуться на объём первой; задержка первого прохода
  добивки от финала родителя 3-4 мин, повтора — 5-6 мин.
- ⚠️ Остаточный риск (пре-существующий, НЕ регрессия правки): при ПУСТОМ `agency` в body
  agency-гейт не применяется (`_agency_gate_claim`: `if not agency: return True`) → сёстры
  теоретически могут пойти параллельно на одном логине. Уточнение проверяющего: **в-процессная
  гонка ЗАКРЫТА** (`_CREATE_ACTIVE_AGENCIES[""]` работает как обычный ключ словаря и сериализует
  сёстры внутри процесса); реальный остаток — **кросс-процессный** зазор, существовавший и до
  разбивки. Закрывается только per-login гейтом в `_claim_next_job` — это уже не точечная правка.
- НЕ помогло ранее: (нет — первая правка этой сигнатуры)

### COPY_PROMO_CSRF_COLD — первое grid-промо кампании падало «тихим null» (2026-07-18)
- Симптом: `promo_attached` mismatch на 12 из 13 кампаний; промо (напр. 1913869 на 12 РК) не создаётся, `PromoClient.add` → (None, None): ни id, ни validation-ошибок.
- Root-cause: первый POST на свежем `UacClient` уходит БЕЗ `x-csrf-token` (grid молча отдаёт `data:null`), `_absorb_csrf` подхватывает токен только ИЗ ответа. Промо самой массовой кампании идёт первым → всегда cold. **НЕ про RUB/amount=1000000** — опроверг live (warm-add с теми же полями создаёт промо).
- Решение (`promo.py`, `ac68625`): `_ensure_csrf()` — тёплый `query{__typename}` до первой мутации `add`/`attach`; + retry-on-empty в `add`.
- ✅ подтверждено run 20 (2026-07-18): promo_attached зелёный, 2 промо на 12 привязок.

### COPY_CALLOUT_UNION_OVERADD — кампания с 0 уточнений у источника получала union из 8 (2026-07-18)
- Симптом: `callout_count` mismatch src=0 tgt=8 на кампании, которой нет в `campaign_callouts.json`.
- Root-cause: `step_attach_callouts` падал в `fallback_union` для КАЖДОЙ отсутствующей в связи кампании; но непустой файл связи = связь полная → отсутствие = реально 0 уточнений.
- Решение (`copy_steps.py`, `deeb10b`): union только при ПУСТОМ файле связи (глоб. фолбэк); известная связь без записи → ничего не вешаем.
- ✅ подтверждено run 20: callout зелёный, фолбэк-union 0.

### COPY_GRID_TYPENAME_FLAKY — grid соврал «все Unified» → битый CopyCamp-снапшот + падение после delete_drafts (2026-07-18)
- Симптом: `Grid CopyCamp: Invalid syntax offending token '<EOF>' at column 305`; падение на grid-снапшоте источника, цель уже очищена delete_drafts. Детерминированно, но только когда `_grid_list_campaigns` вернул все выбранные как `GdUnifiedCampaign` (реально TEXT_CAMPAIGN по v5).
- Root-cause: grid-typename нестабилен (наблюдалось 13-Unified ↔ 0 строк). Роутинг брал grid-cookie путь если ВСЕ unified → неверно для текстовых.
- Решение (`copy_engine.py`, `f4d9b05`): v5-кросс-чек Type перед grid-unified fast-path; TEXT/DYNAMIC/APP/SMART исключаются из `selected_unified_rows` → идут v5-pull. Настоящие ЕПК/UAC v5 не отдаёт → grid-путь сохраняется.
- ✅ подтверждено run 19/20: прошли v5-путём, без CopyCamp-падения.

### COPY_KW_SUBCOPY_NO_HEAL — крупные tp2/Поиск кампании под-копировались, auto_repair не лечил ключи/ссылки (2026-07-18)
- Симптом: `keyword_count` src=2705 tgt=28 / src=8600 tgt=2326 + `sitelinks_present` False на 2 кампаниях; аплоад рапортует failed=0, `copy_repair: repairs=0`.
- Root-cause: (1) v5 `keywords.add` вернул truthy Id но ключи не осели (под-копирование в момент создания кампании; перемежается). ⚠️ Диагноз «v5 фантомит на поиске» ОПРОВЕРГНУТ live — add оседает; лимит API 1000 ключей/запрос (код 9300). (2) `run_copy_repair` НЕ имел ремонтёра keyword_count/sitelinks → пропуск оставался.
- Решение (`copy_verify.py`+`copy_engine.py`, `874dff7`): `_repair_keywords` — live `keywords.get` по кампании vs источник (та же гео-морфа), дозалив недостающего ≤900 батч; + идемпотентный sitelinks-retry `step_attach_sitelinks`.
- ⚠️ ЧАСТИЧНО: sitelinks-retry работает; но keyword-self-heal на момент verify не срабатывал — см. следующую запись (вырезание ПОСЛЕ verify). «117/117» run 20/22 оказалось МАСКОЙ (verify зелёный, а ключи вырезаны позже).

### COPY_KW_POST_VERIFY_STRIP — Яндекс вырезает ключи свежих черновиков ПОСЛЕ прохождения verify (2026-07-19)
- Симптом: verify зелёный (keyword_count src=tgt), но ЖИВОЙ v5-счёт через ~10-20 мин показывает недокоп (6/13 в run 22, 2/13 в run 23). Ловится ТОЛЬКО живой 1:1-проверкой, не verify.
- Root-cause: verify читает keyword_count из v5 авторитетно (copy_verify.py:466) — на момент сверки ключи РЕАЛЬНО были, Яндекс вырезал их ПОЗЖЕ. Ключи, залитые в ОКНО СОЗДАНИЯ кампании, частично вырезаются; ре-add в ОСЕВШУЮ кампанию держится (проверено монитором 20 мин). Батч 900 усугублял. НЕ баг verify, НЕ фантом-Id.
- Решение (`d527832`): `_copy_delayed_reverify` гоняет цикл repair→пауза→re-verify после осевшей сверки; выход по 2 чистым кругам подряд И после `_COPY_HEAL_MIN_SEC`=20 мин. + откат keyword-батча 900→200.
- ⚠️ ЧАСТИЧНО (run 23 живьём): 11/13 кампаний = точное 1:1. Две МЕГА-кампании (>~6k ключей: 9958→5759, 8600→1009) НЕ добиваются — часть ключей вообще без target-группы (баг маппинга групп на крупных) + остаток мгновенно вырезается даже при ручном ре-add на осевшей. Гипотеза: **платформенный потолок Яндекса на большие наборы ключей в черновике** (задело бы и реального клиента). Решение Семёна: зафиксировать 11/13, мега — отдельно (не блокер).

### GRID_UPDATE_ADS_NULL_ITEMS_FALSE_SUCCESS — отказ Директа считается успехом, «заменено N» при 0 изменений (2026-07-19)
- Симптом: вкладка «Смена изображения» рапортует `{"replaced": 15, "errors": []}`, а в аккаунте
  НЕ ИЗМЕНИЛОСЬ НИЧЕГО (независимый read-back: 0 расхождений из 121 объявления).
- Где: `grid_finalize.update_text_ad_images:2826` и `grid_finalize.update_ad_images:2478` —
  обе возвращают `len(res.get("updatedAds") or [])`.
- Root-cause: при отказе Директ отдаёт `updatedAds` **списком той же длины из `null`**, а причины
  кладёт в `validationResult.errors`. `len([null]*15) == 15` → отказ = полный успех. В
  `update_text_ad_images` ошибки только ПЕЧАТАЮТСЯ в stdout и наверх не идут; в `update_ad_images`
  не читаются вовсе. Гейт `upd_text < len(text_items)` не срабатывает — счётчик уже равен длине.
- Доказано живьём (probe 4, `porg-gcegsszl`, кампания 704132838): 15/15 items отклонены
  `BannerDefectIds.Gen.ACTION_IN_ARCHIVED_CAMPAIGN`, HTTP 200, `updatedAds: [null ×15]`,
  прод-функция вернула `replaced: 15, errors: []`. Нарушает инвариант CONTENT_EDITOR.md
  «возвращать реальное число изменённых объектов».
- Решение (ПРИМЕНЕНО 2026-07-19, локально, live НЕ прогонялось): `_grid_updated_ad_ids` считает
  только элементы-словари с непустым `id`; `_grid_validation_reasons` собирает
  `validationResult.errors`+`warnings`+GraphQL-`errors` в строки `CODE @path (params)`;
  `GridClient._note_ad_update_shortfall` кладёт причину в `self.last_ad_update_errors`, откуда
  `content_images_routes._grid_update_reasons` подмешивает её в `errors` задания. Оба метода
  (`update_ad_images`, `update_text_ad_images`) + их `except` возвращают причину, а не тихий 0.
  ⚠️ Правка меняет счётчик и на успешном пути → гнать через `direct_verifier`.
  Тесты: `tests/test_content_images_transport_split.py` (24, из них 8 новых; 5 из новых падают
  на до-фиксовом коде — проверено временным реверсом).
- Побочно (не баг, знать надо): **архивная кампания отклоняет `UpdateTextAds` целиком** —
  `ACTION_IN_ARCHIVED_CAMPAIGN`. Выбирать архивную кампанию «чтобы безопаснее» для probe нельзя:
  запись туда невозможна в принципе.
- Дополнение по job `ce_1809d73d5dd9` (2026-07-19/20): один `UpdateTextAds` на 450 items смешал
  архивные и неархивные объявления. Grid обновил 45, вернул `ACTION_IN_ARCHIVED_CAMPAIGN` только
  по первым 5 индексам, а 44 неархивных `GdTextAd` остались со старым хэшем без понятной причины
  в результате job. Live-retry тем же payload показал точную причину: все 44 — ad-level archive
  внутри неархивных STOPPED-кампаний `705785854`/`705785910`, Grid вернул
  `BannerDefectIds.Gen.CANNOT_UPDATE_ARCHIVED_AD`.
- Системный фикс: `_rsya_inventory` исключает архивные кампании из write-set (`skipped:
  архивные кампании/объявления не изменяются`), `update_ad_images` / `update_text_ad_images`
  режут мутации на `_GRID_MUTATION_CHUNK=50` и добавляют в ошибку полный `failed_ad_ids=...`
  по каждому чанку. `_replace_rsya_images` классифицирует чистый `CANNOT_UPDATE_ARCHIVED_AD`
  как `ads_archived`, а не `errors`: архивные объявления по решению Семёна не изменяем.
- Статус: ✅ подтверждено live-retry 2026-07-20 на `ce_1809d73d5dd9`: оставшиеся 44 — архивные
  объявления, доретраивать их не нужно; старый хэш в них является ожидаемым остатком.
- НЕ помогло ранее: — (первая правка этой сигнатуры).

### UAC_PATCH_TP7_FEED_ID_REQUIRED — UAC full PATCH невозможен на товарных кампаниях (2026-07-19)
- Симптом: UAC-лег вкладки на `tp7`-кампании → `HTTP 400 {"validation_result":{"errors":[
  {"path":"feedId","code":"DefectIds.CANNOT_BE_NULL","text":"Необходимо заполнить поле"}]}}`.
  Картинки в `contents` товарной кампании не заменяются вообще ни разу.
- Где: `routes_content_editor._UAC_PATCH_FULL_KEYS` / `_uac_campaign_patch_payload:614`.
- Root-cause: `feedId` в списке ключей билдера нет, а для товарной кампании он NON_NULL.
  Имеющийся эталон `_har/UAC_image_replace.json` снят с **МК (tp6)**, где фида нет, — поэтому
  пред-полётная сверка «браузер шлёт, мы нет» даёт `[]` и дефект не ловит.
- Доказано живьём (probe 4, кампания 705785965). Смягчающее: отказ ЧЕСТНЫЙ — ошибка попала в
  `errors` задания, `replaced`=0, `touched_ids` пуст, частичной записи нет (detail: 0 изменений).
  Но `legs_reconcile` этого не ловит: `GdTextAd` товарки Grid-лег обслужил, `left_to_uac_cids` пуст.
- Решение (2026-07-19, применено): фид уже был в whitelist, но ветка «`keywords is None` →
  обнулить фид (MUST_BE_NULL для МК-автотаргетинга)» срабатывала и на товарке — живой detail
  товарки отдаёт `keywords: null` (в PATCH-теле браузер шлёт `keywords: []`), и код вырезал все 4
  фид-ключа. Фикс `routes_content_editor._uac_campaign_patch_payload`: обнуление фида обёрнуто в
  `if not detail.get("ecom")` — товарка (`ecom: true`) фид сохраняет, МК (`ecom: false`) по-прежнему
  обнуляет. `ecom` — тот же флаг, по которому браузер решает, слать ли фид.
- Эталон снят: HAR-66 entry 120 (PATCH товарной 708395627, 200, 38 ключей) →
  `_har/UAC_ecom_feed_replace.json` + раздел «Товарные кампании» в `_har/IMAGE_REPLACE_schema.md`.
  feed_id=listings_feed_id=2611255 (ОДНО значение под ДВУМЯ ключами, оба из raw detail раздельно);
  feed_filters=listings_feed_filters=[{"conditions": []}].
- Верифицировано READ-ONLY (мутаций 0, `porg-gcegsszl`, товарка 708395627): live detail →
  билдер. ДО фикса payload 35 ключей, MISSING feed_id/listings_feed_id/feed_filters/
  listings_feed_filters. ПОСЛЕ: 39 ключей, MISSING 0 vs HAR, feed-значения совпадают 1:1,
  content_ids 5/5 с порядком. EXTRA `reserve_landing_id: null` — benign (браузер на товарке не
  шлёт; под REPLACE null = no-op; нужен для МК). МК-путь инертен: `ecom=false` → ветка обнуления
  фида работает как раньше (проверено на `_har/UAC_image_replace.json`).
- Статус: 🟡 **фикс сделан, ждёт живого прогона записи** (READ-ONLY-сверка зелёная, но реальный
  PATCH товарки после фикса ещё НЕ отправлялся).
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не «чинить» добавлением `feedId: null` —
  поле NON_NULL, нужен реальный id фида. Не различать ветки по «пусто ли `keywords`» — у товарки
  `keywords` пусто ТОЖЕ; единственный надёжный различитель — `ecom`.

### GRID_TEXTAD_IMAGE_NOT_SUPPORTED — вкладка «Смена изображения» не видела обычные текстовые объявления (2026-07-19)
- Симптом: на `porg-gcegsszl` вкладка показывала 54 карточки при 7531 `GdTextAd` (7501 с картинкой,
  87 различных хэшей) — весь основной массив аккаунта молча уезжал в `skipped` строкой
  «не адаптивные объявления». На `porg-bzti5ud7` — 1655 таких же объявлений.
- Где: `content_images_routes._rsya_inventory` / `_replace_rsya_images` (читались только
  `GdAdaptiveTextAd`), `grid_finalize` (метода записи для TextAd не было вовсе).
- Root-cause: у `GdTextAd` ДРУГАЯ мутация и ДРУГАЯ форма картинки — `UpdateTextAds`
  (`updateAds(input:GdUpdateAdsInput!)`) и **`textBannerImageHash` СКАЛЯРОМ**, а не
  `UpdateAdaptiveTextAds` + список `imageHashes`. Читается картинка как `bannerImage` (одна),
  не `images[]`. Эталон — `_har/TEXTAD_image_replace.json` (снят Семёном).
- Решение (2026-07-19): `GridClient.text_ads_for_update` (RMW-чтение, переиспользует
  `_ads_rows_paginated`/`_grid_inheritable_write`/`_grid_images_rich`) + `update_text_ad_images`
  (`saveDraft:false` — у адаптивных `true`, значение НЕ переносить). Инвентарь и ветка замены
  разделяют пути по `kind`.
- ⚠️ Асимметрии write-формы для TextAd (тот же класс, что `UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY`):
  визитка пишется ОБЪЕКТОМ `permalinkWithPhone{policy,…}` (у адаптивных — плоскими
  `permalinkId`/`phoneId`), `turboGalleryHref` (скаляр на чтении) пишется объектом
  `turboGalleryParams{turboGalleryHref}`, `linkTail`→`displayHref`, `assetValue`→`calloutIds`/
  `sitelinkSetId`. Входной тип называется **`GdUpdateAdInput`** (**32 поля** — живая интроспекция
  2026-07-19; ранее в этой записи стояло ошибочное «33», по знаменателю считают покрытие),
  `GdUpdateTextAdInput` в схеме НЕТ. `turbolandingId`/`turbolandingHrefParams`/`multicards`
  вернуть нечем → объявление с ними ПРОПУСКАЕТСЯ (`rmw_unsafe`), а не переписывается с потерей.
- Статус: 🟡 **ждёт живого прогона.** Сверка payload с эталоном: MISSING 0 / EXTRA 0, 17/17 ключей —
  ⚠️ и это теперь верно для **ВСЕГО корпуса**, а не только для ветки «поля непусты». Раунд 2
  (2026-07-19, зонд с перехваченной мутацией на 7501 объявлении `porg-gcegsszl`): было **два**
  варианта состава — 2531 объявл. слали 17 ключей, а 4970 слали 15 (без `adPrice` и
  `permalinkWithPhone`, т.к. оба ключа опускались при пустом значении), тогда как браузер шлёт их
  ВСЕГДА. Эквивалентность «ключа нет» ≡ `{"policy":"CLEAR"}` под REPLACE не доказана, поэтому
  опираться на неё перестали: оба ключа шлются безусловно (`adPrice: null` — тип `GdAdPriceInput`
  nullable по интроспекции; `permalinkWithPhone: {"policy":"CLEAR"}` — ровно браузерная форма,
  `policy` внутри объекта NON_NULL). Замер ПОСЛЕ: **7501/7501 объявлений = один состав, 17 ключей**.
  ⚠️ Знаменатель честно: в корпусе HAR **3 запроса `UpdateTextAds` 200, но все — ОДНО объявление**,
  т.е. эталон фактически ОДИН; недостаток выборки закрыт живой интроспекцией входного типа.
  Не покрыто эталоном до сих пор: `adPrice: null` (у браузерного объявления цена была непуста) —
  первый живой probe обязан пройти именно по объявлению БЕЗ цены.
  Мутаций в Директ НЕ отправлялось. Отчёт: `.claude/sdd/images-tab-textad-report.md`.
- **✅ ПОДТВЕРЖДЕНО ЖИВЬЁМ (probe 4, 2026-07-19, `porg-gcegsszl`, кампания 705785965).**
  `UpdateTextAds` отработал именно в требуемой ветке **БЕЗ `adPrice`**: 15 items, HTTP 200,
  `updatedAds` = 15 РЕАЛЬНЫХ id, `validationResult: null`. Директ принял оба спорных ключа —
  `adPrice: null` и `permalinkWithPhone: {"policy":"CLEAR"}`, т.е. 17-ключевой безусловный
  состав верен. Независимый read-back (собственный селект ~50 полей, НЕ `text_ads_for_update`):
  по всей кампании изменились ровно 2 поля на 15 целевых объявлениях — `bannerImage` (цель) и
  `status` ACTIVE→MODERATION (перемодерация после смены картинки, неизбежна). **Сохранены:**
  `inheritableSitelinkSet` OVERRIDE, `inheritableCallouts` CLEAR, `sitelinks`, `disclaimer`/
  `dynamicDisclaimer`/`disclaimerCategories`/`flags` (финансовые дисклеймеры!), `href`/`hrefParams`/
  `domain`, `linkTail`, `permalinkWithPhone`, `turbolanding`/`multicards`, `button`, `logoImage`,
  `typedCreative`. Соседние 249 объявлений — 0 изменений. Откат обратной картой без повторной
  заливки: `bannerImage` вернулся, расхождений с ДО только `status`.
  Отчёт: `.claude/sdd/probe4-gcegsszl-report.md`.
- ⚠️ **Ветка `rmw_unsafe` НИ РАЗУ не обкатана:** турболендингов/мультикарточек на обоих аккаунтах
  0 (`porg-gcegsszl`, `porg-pvrbl7mh`). Решение (пропускать, а не переписывать с потерей) верное,
  но код мёртвый — **первый живой турболендинг встретит непроверенный путь**.
- Исключены намеренно (решение Семёна + живой замер): `GdShoppingAd`/`GdListingAd`/`GdSmartAd`/
  `GdMlAutoSuggestAd` — по фиду (`feed{id}` непуст у 100%; у ML ещё и
  `imageGenerationTypes=[GENERATED_IMAGE,SITE_IMAGE]`, своего хэша нет); `GdDynamicAd` (0/95) и
  `GdPostAd` (0/38) — `bannerImage` пуст у всех, заменять нечего.
- НЕ помогло ранее: включение поисковых кампаний в чтение (прошлый заход) — карточек не прибавило,
  потому что в поиске нет АДАПТИВНЫХ объявлений, там именно `GdTextAd`; лечить надо было тип
  объявления, а не фильтр кампаний.

### UAC_OWNED_TEXTAD_NO_TRANSPORT — `GdTextAd` в UAC-владеемых кампаниях оставался без транспорта записи (2026-07-19)
- Симптом: 6 карточек инвентаря (`porg-gcegsszl`) показывались как `supported: true`, но задание по
  ним падало ошибкой «картинка только в tp6/tp7-кампаниях, но её нет в contents». Плюс 23 из 34
  UAC-владеемых кампаний не проверялись вообще ничем: `_verify_uac_mirror` считает только
  `GdAdaptiveTextAd`, а адаптивных у них нет → `checked=0`, `stale=[]`, задание рапортует чисто.
- Где: `content_images_routes._replace_rsya_images` / `run_image_replace` (запрет Grid-лега
  действовал на КАМПАНИЮ целиком) и `_verify_uac_mirror:640`.
- Root-cause: допущение «объявление в UAC-кампании = проекция `contents`, поэтому Grid-лег не
  нужен» выводилось ТОЛЬКО из адаптивных и было молча распространено на `GdTextAd`.
  **Живой read-only замер (мутаций 0, per-campaign сверка Grid-хэшей с `contents`) его опроверг:**
  * адаптивные в UAC-владеемых кампаниях — `grid_only = 0` (11 кампаний) → проекция, допущение верно;
  * `GdTextAd` там же — **`grid_only = 34` хэша в 6 кампаниях** → объявление несёт картинки,
    которых в `contents` НЕТ ВОВСЕ, т.е. проекцией оно не является и UAC-PATCH `content_ids`
    до `bannerImage` не доходит.
  Состав UAC-владеемых: 2952 `GdTextAd` против 11 `GdAdaptiveTextAd`; кампании не пересекаются
  (11 с адаптивами + 23 только с текстовыми = 34).
- Решение (2026-07-19): владение UAC блокирует Grid-лег **по ТИПУ объявления, а не по кампании** —
  новый `_grid_transport_scan`. Адаптив в UAC-владеемой кампании по-прежнему отдаётся UAC-легу
  (доказанная проекция, двойной записи в один объект нет), `GdTextAd` пишется Grid'ом всегда
  (`UpdateTextAds` — ДРУГОЙ объект, не тот, что PATCH'ит UAC). Транспорт считается уже на этапе
  ИНВЕНТАРЯ (`_annotate_transport`) → карточка без транспорта приезжает `supported:false` с
  причиной, а не выясняется ошибкой после постановки задания. `_verify_uac_mirror` отдаёт
  `no_adaptive`, чтобы `checked: 0` не читалось как «всё чисто».
- Замер ДО→ПОСЛЕ (`porg-gcegsszl`, 87 ключей): `GRID_OK 59→87`, `UAC_OK 22→0` (у этих ключей теперь
  есть И Grid-работа: contents пишет UAC, текстовые объявления — Grid), **`NO_TRANSPORT 6→0`**,
  `supported:false` после аннотации — 0 карточек. Подтверждённое не поехало: merged 87, UAC 42,
  по фиду 1804, «не поддерживается» 133.
- Статус: ✅ **ПОДТВЕРЖДЕНО ЖИВЬЁМ** (probe 4, 2026-07-19, `porg-gcegsszl`, кампания **705785965**
  `tp7_cpc_site — TK_RA_bu_fresh_cpa_new`, cost 0 / shows 0). Запись `UpdateTextAds` ВНУТРЬ
  UAC-владеемой кампании выполнена и откачена. Главное — допущение подтверждено ФАКТОМ:
  Grid заменил `bannerImage` у 15 текстовых объявлений, а **`contents` кампании не изменились
  ВООБЩЕ** (UAC detail: 0 изменившихся ключей) — значит `bannerImage` у `GdTextAd` действительно
  НЕ проекция `contents`, и Grid-лег обязан их писать. Интерлив-снимок между легами
  (обёртка вокруг `_replace_uac_images`, прод-логика не менялась) показал: старого хэша уже 0
  ДО того, как UAC-лег стартовал.
  ⚠️ **«Кто пишет последним» при ОБОИХ успешных легах живьём НЕ наблюдалось** — UAC-лег в этой
  кампании упал HTTP 400 (см. `UAC_PATCH_TP7_FEED_ID_REQUIRED` ниже). Наблюдаемо только после
  фикса `feedId`. Отчёт: `.claude/sdd/probe4-gcegsszl-report.md`.
- НЕ помогло ранее: — (первая правка этой сигнатуры). ⚠️ Не переизобретать: **не** возвращать
  запрет Grid-лега на всю кампанию целиком (это и есть исходный дефект) и **не** чинить дыру
  добавлением `GdTextAd` в `_verify_uac_mirror` — проверять там нечего, UAC-PATCH до текстовых
  объявлений не доходит; лечится транспортом, а не проверкой.

### IMAGES_TAB_UPLOAD_413_SILENT — загрузка картинки >1МБ падала 413 на nginx, причина скрыта от админа (2026-07-19)
- Симптом: в модалке «Замена изображения» после выбора файла >~1 МБ инфо-строка навсегда
  оставалась «не загружен», кнопка «Показать, что изменится» не активировалась; текст
  подсказки — generic «Загрузите новый файл, чтобы продолжить», без причины отказа.
- Где: nginx `location ^~ /direct/api/content-editor/` (`deploy/nginx-direct-location.conf` +
  живой `/etc/nginx/sites-enabled/seoadvanced.ru`) и `static/direct/content_editor.js:ceImgModalUpload`.
- Root-cause (два независимых дефекта в одной цепочке):
  1. У location не было `client_max_body_size` → nginx откатывался к дефолту 1m и рубил тело
     запроса ДО проксирования во Flask (413 из самого nginx, `errors.log`: `client intended to
     send too large body: 2454855 bytes ... POST /direct/api/content-editor/images/upload` —
     ровно размер файла со скриншота Семёна). Бэкенд (`content_images_routes._MAX_BYTES`) и так
     допускает 40 МБ — ограничение было исключительно на уровне прокси, невидимое в коде фичи.
  2. Даже когда `r.error` есть, `ceImgModalUpload` писал его в `#ceimg-modal-note` НАПРЯМУЮ, но
     тут же в `finally` вызывал `ceImgModalRenderNew()`, которая безусловно перезаписывала note
     обратно на generic-текст — реальная причина отказа никогда не долетала до экрана ни при
     этой ошибке, ни при любой другой ошибке загрузки.
- Решение: `client_max_body_size 50m; client_body_timeout 120s;` на этот location (репо + живой
  конфиг, `nginx -t` + `reload`); `CEIMG.modal.error` — отдельное поле состояния, `ceImgModalRenderNew`
  показывает его вместо generic-текста, если не идёт загрузка; очищается при новой попытке/открытии
  модалки. Заодно кнопка «✕» на превью нового файла — можно убрать выбор без ожидания сервера.
- Детект-запрос: `grep 'too large body.*content-editor/images/upload' /var/log/nginx/error.log`.
- Статус: ✅ **ПОДТВЕРЖДЕНО ЖИВЬЁМ** (2026-07-19) — до фикса `curl` с телом 2,5 МБ на прод-URL
  давал `413`; после фикса (`nginx -t` ok, `reload`) тот же запрос дошёл до Flask (`401`, авторизация,
  что и ожидаемо для неавторизованного curl — тело нижней границы 1 МБ больше не режется).
- НЕ помогло ранее: — (первая правка этой сигнатуры).

### GRID_RMW_AD_ASSETS_WIPED — RMW `update_ad_images` стирал ad-level набор быстрых ссылок / кнопку (2026-07-18)
- Симптом (потенциальный, найден по HAR ДО инцидента): у объявления с СОБСТВЕННЫМ набором быстрых
  ссылок (`inheritableSitelinkSet.policy=OVERRIDE`) после любого RMW-обновления картинок привязка
  сбрасывается на наследование от кампании; кнопка «Получить скидку» пропадает.
- Где: cookie/Grid, `grid_finalize.GridClient.update_ad_images` (UpdateAdaptiveTextAds = full-replace),
  RMW-чтение `grid_finalize.GridClient.adaptive_ads_for_update`.
- Root-cause: тот же класс, что `GRID_RMW_DISPLAY_HREF_WIPED` — payload **хардкодил**
  `inheritableCallouts/inheritableSitelinkSet = {"policy":"INHERIT"}` и вовсе не слал `button`,
  а мутация REPLACE'ит payload целиком. Живой HAR браузера (`direct.yandex.ru.62har.har`, entry [187],
  200) в том же вызове шлёт РЕАЛЬНОЕ состояние: `{"policy":"OVERRIDE","sitelinkSetId":"1494667558"}`,
  `inheritableCallouts {"policy":"CLEAR"}`, `button{action,href}`. Асимметрия имён (как
  linkTail→displayHref): читается `assetValue`, пишется `sitelinkSetId`.
- Решение (2026-07-18): `adaptive_ads_for_update` дочитывает `inheritableCallouts{policy assetValue}`,
  `inheritableSitelinkSet{policy assetValue}`, `permalinkWithPhone{permalinkId phoneId policy}` и
  отдаёт их уже в WRITE-shape (`_grid_inheritable_write`); `update_ad_images` кладёт их, `button` и
  `permalinkId/phoneId` as-is, а хардкод INHERIT остался ТОЛЬКО как fallback для вызывающих, которые
  состояние не читают (`repair_media`) — поведение не хуже прежнего.
- Статус — **РАЗНЫЙ ПО ТРАНСПОРТАМ. «✅» ниже относится ТОЛЬКО к Grid-пути, не к записи целиком:**
  - **Grid-путь (`update_ad_images` / `UpdateAdaptiveTextAds`) — ✅ ПОДТВЕРЖДЕНО ЖИВЬЁМ 2026-07-19.**
    Доказано измеренно: 16/16 полей целевого объявления и 35/35 строк кампании байт-идентичны
    снимку ДО после отката.
  - **UAC-путь (full PATCH, `_uac_campaign_patch_payload`) — ❌ ОПРОВЕРГНУТО ТЕМ ЖЕ ЖИВЫМ ПРОГОНОМ
    2026-07-19** (раньше здесь стояло ✅ — статус ОТКАЧЕН). Основание прежнего ✅ — «снимки совпали
    побайтово» — неверно: сверялись **28 полей из 127**, и селектом УЖЕ, чем снимок ДО. Независимая
    сверка ВСЕХ 127 ключей вскрыла **новый экземпляр ровно этого класса** (потеря/подмена ad-level
    состояния) и **два неоткаченных изменения на РАБОЧЕМ клиентском аккаунте**. Детали —
    `UAC_FULL_PATCH_SIDE_EFFECTS_OUTSIDE_PAYLOAD` ниже. По UAC-пути живой прогон дал НЕ
    подтверждение сохранности, а её опровержение. ⚠️ Это **не** значит «UAC-путь выключить»:
    у МК нет API, куки-PATCH — единственный транспорт, побочные эффекты приняты как его цена
    (решение Семёна). Значит только одно: **утверждать сохранность ad-level полей по UAC-пути
    больше нельзя** — её надо каждый раз мерить полной сверкой detail ДО/ПОСЛЕ.
- Grid-часть, доказательства (probe с записью, `porg-pvrbl7mh`, объявление
  `1915813972121417716` кампании `712849028`, cost=0/shows=0; прод-путь `run_image_replace`).
  `UpdateAdaptiveTextAds` HTTP 200, `updatedAds` непуст, `validationResult: null`; независимый
  read-back (свой GraphQL-селект, НЕ `adaptive_ads_for_update`) показал сохранность ПОЛЕ В ПОЛЕ:
  `inheritableCallouts {"policy":"OVERRIDE","assetValue":["43516097","43516099","43516104"]}`,
  `linkTail "Авто-в-Краснодаре"`, `bannerPrice 315000.00/770900.00/FROM/RUB`,
  `inheritableSitelinkSet OVERRIDE "1492866238"`; 34 соседних объявления кампании — 0 изменений;
  откат вернул все 35 строк к снимку ДО побайтово. **Главное: write-форма
  `{"policy":"OVERRIDE","calloutIds":[…]}` держалась только на интроспекции схемы — теперь она
  отправлена живьём и принята Директом.** Отчёт: `.claude/sdd/probe3-pvrbl7mh-report.md`,
  снимок ДО: `.claude/sdd/probe3-pvrbl7mh-before.json`.
  ⚠️ **НЕ покрыто живой записью (объектов нет на аккаунте, не «проверено и ок»):**
  (а) `button.customText` — `button` пуст у 0/2794 адаптивных объявлений `porg-pvrbl7mh`
  (`hasButton=false` у всех), нужен аккаунт с кнопкой и кастомной надписью;
  (б) `creativeIds` (видео) на Grid-пути — видео есть ровно у 1 объявления из 2794, и оно в
  UAC-владеемой кампании, которую Grid-лег намеренно пропускает (`skip_cids=uac_owned`,
  `content_images_routes.py:588`), т.е. `update_ad_images` по нему не вызывается никогда;
  сохранность видео доказана только через UAC full PATCH (`typedCreatives` 2/2 до и после).
  Оффлайн-доказательство (прежнее): реконструкция payload'а из HAR-ответа чтения совпала с тем,
  что слал браузер, ПОЛЕ В ПОЛЕ; отличие только в представлении пустой визитки (мы
  `permalinkId/phoneId:null`, браузер `permalinkWithPhone{policy:CLEAR}` — семантически то же).
- Доработка 2026-07-18 (по ревью, тот же класс потерь — ещё 3 поля): (1) **`inheritableCallouts`
  БОЛЬШЕ НЕ отдаётся `None`** — write-shape ПОДТВЕРЖДЕНА: интроспекция живой схемы даёт
  `GdInheritableCalloutsInput{calloutIds:[ID] calloutsIds:[ID] policy:GdAssetInheritancePolicyInput!}`,
  каноничное имя — **`calloutIds`** (оно есть в 20+ input-типах схемы: `GdUpdateAdaptiveTextAdInput`,
  все `GdAdd*AdInput`/`GdUpdate*AdInput`, `GdDeleteCalloutsInput`; `calloutsIds` встречается ровно в
  двух типах — `GdCalloutsInput` и `GdInheritableCalloutsInput` — и всегда дублем того же типа рядом
  с `calloutIds`, т.е. легаси-алиас). Живой probe `porg-pvrbl7mh`: 102/102 адаптивных объявления несут
  `{"policy":"OVERRIDE","assetValue":["43516097","43516099","43516104"]}`, т.е. прежний `None` стирал
  уточнения у ВСЕХ. ⚠️ `assetValue` у уточнений — **СПИСОК** id (у набора быстрых ссылок — скаляр),
  `_grid_inheritable_write` теперь выводит форму значения из самого значения.
  (2) `displayHref` не слался вовсе → RMW стирал отображаемую ссылку (тот же probe: linkTail непуст
  102/102). (3) `button.customText` не читался и не слался → обнулялся текст кастомных кнопок.
- НЕ помогло ранее: — (неудачных попыток не было).

### IMG_SSHFS_READ_HANG — зависание на чтении картинки с sshfs без таймаута (2026-07-18)
- Симптом: прогон создания РК стоит без прогресса, watchdog убивает джобу (`running без прогресса
  > 20 мин`), created 3/20 вместо 20/20 при том, что тот же набор до этого давал 20/20 за 21.8 мин.
  Джобы `f58a123d8405`, `a4bef725b5cb` (два подряд).
- Сигнатура (live-стеки `/tmp/direct_stall_1784390810.trace`, `/tmp/direct_stall_1784395039.trace`
  на LXC101): **12-14 потоков** одновременно стоят в
  `grid_finalize.py upload_image` ← `create_set_feeds.py:555 _cached_upload_image` на голом
  `fh.read()`; рядом — `create_set_feeds.py:602 _parallel_upload_images` в `isfile`,
  `create_set_feeds.py:526` в `posixpath.realpath`, `create_set_assets.py:44 _manual_creative_paths`
  в `os.listdir`. Минус-слова/`_collect_pack_minus` в трейсах ОТСУТСТВУЮТ (проверено grep) — не они.
- Root-cause (доказан фактом): `_manual_creative_paths` (create_set_assets.py) ветка 1 отдавала пути
  на **`/opt/creatives/Manual/{ct}/`**, а это НЕ локальный диск (как утверждал комментарий), а
  **sshfs-монт M3**: `mount` → `m3-relay:/Users/Shared/agency/creatives on /opt/creatives type
  fuse.sshfs`. У FUSE-операций таймаута НЕТ by design → при подтормаживании моста к M3 `read`/
  `stat`/`listdir` висят бесконечно. `signal.alarm` неприменим (работает только в главном потоке,
  а весь аплоад — в ThreadPoolExecutor). При этом ТЕ ЖЕ файлы лежат локально в зеркале
  `NEURO_PACK_MOUNT=/opt/neuro_content_local/_manual/{ct}/` (ночной `sync_content_m3.py`).
  Замер на живом монте: чтение одного PNG ~2МБ — **sshfs 8.8-9.5 с против 0.001-0.002 с локально**
  (~5000×), `ls` одной ct-папки по sshfs — 26 с. Байты идентичны (`b_local == b_sshfs` → True).
- Решение (2026-07-18), 6 файлов:
  1. `create_set_assets.py::_manual_creative_paths` — зеркало `_manual/{ct}` ПЕРВЫМ, sshfs-монт
     только фолбэком, если ct в зеркале нет.
  2. `kontent_pack.py` — новый слой ФС-операций с пределом времени: `fs_call_bounded` (операция в
     отдельном daemon-потоке + `join(timeout)`; застрявший поток снять нельзя — он в непрерываемом
     syscall, но ВЫЗЫВАЮЩИЙ освобождается и получает default) + `read_bytes_bounded` /
     `isfile_bounded` / `realpath_bounded` / `listdir_bounded` / `isdir_bounded`.
     Лимит `NEURO_FS_OP_TIMEOUT` (20 с), защита от размножения потоков `NEURO_FS_STUCK_MAX` (16).
  3-6. Все точки из стека переведены на bounded-операции: `grid_finalize.upload_image`
     (isfile + read; недоступен → `[img-upload] SKIP` и `None`, картинка пропускается),
     `create_set_feeds.py` (realpath ключа кэша + isfile в `_parallel_upload_images`),
     `uac_client.upload_image_file` (read; при таймауте `UacApiError` — вызывающий пропускает
     ОДНУ картинку, не роняя черновик), `blueprint_content_rules._manual_rule_lookup_key`
     (распознаёт ОБА корня → ключ правила вкладки «Контент» тот же, что раньше).
- Набор загружаемых картинок НЕ изменился (проверено): ct-папок в зеркале и на sshfs 199 == 199,
  множества имён идентичны; на 5 ct старый и новый путь дают одинаковый список basename на аплоад
  (старый возвращал 8 записей = каждый файл дважды под двумя корнями, дедуп по basename ниже по
  потоку давал те же 4 файла — но ЧИТАЛСЯ при этом sshfs-путь, он сортируется первым).
- Проверка предсказуемости: РЕАЛЬНЫЙ файл на РЕАЛЬНОМ sshfs при лимите 1 с →
  `[img-upload] SKIP … файл не прочитан за 1с`, возврат за 1.00 с вместо зависания; блокирующий
  FIFO → `read_bytes_bounded` вернул None ровно за 5.00 с; несуществующий путь → мгновенно None/False.
- ⚠️ НЕ покрыто (осознанно, вне класса): `uac_client.py:221` (md5-дедуп в `collect_image_files` —
  это логика ВЫБОРА картинок) и `uac_client.py:442` (чтение видео) читают файл без предела времени.
  После фикса они работают по локальным путям, в стеках зависания не фигурировали.
- Статус: 🟡 ждёт живого прогона (оффлайн-проверки на LXC101 пройдены, см. выше).

### NONAUTO_CT_NAME_PRIORITY — у не-авто слепка авто-справочник марок перебивал тему структуры (2026-07-18)
- Симптом: ct не-авто слепка (dmp), попавший в авто-диапазон, резолвится в МАРКУ вместо темы →
  `brand` непустой → в B2B-заголовки/тексты течёт авто-лексика, а `_filter_group_keywords(model=brand)`
  режет B2B-ключи. Триггер: разрешение ct0084 («Конкуренты», 303 ключа / 49 групп, porg-mushirne).
- Где: `create_set_text_builders.py:435` (token, tp2), `create_set_tp1_builders.py:860` и `:1747` (cookie).
- Root-cause: `_ag_part1_map()` (`campaign_naming.py:40-80`) — ЕДИНАЯ карта из ДВУХ источников:
  gsheet_naming (авто-марки) + leadgen_ct_naming, причём leadgen добавляет только отсутствующие ct
  (`if ct not in m`). Для не-авто слепка эта карта НЕ его справочник. А все три call-site'а ставили её
  ПЕРЕД структурой: `ct_name.get(ct) or _struct_names.get(ct) or ct` → `_struct_names` молча
  игнорировалась для любого ct из авто-диапазона. Факт с прода: `_ag_part1_map()['ct0084']='Faw Bestune T77'`.
- Решение (2026-07-18): инверсия приоритета в тех же 3 строках → `_struct_names.get(ct) or ct_name.get(ct) or ct`.
  Без хардкода ct/слепка: признак «не-авто» — уже существующий гейт `_struct_ct_names()`
  (`create_set_tp1_builders.py:1611`, `dl.get("auto", True) is False`), для авто-слепков он даёт `{}`
  → ветка `else` (не тронута) работает как раньше.
- Верифицировано (LXC101, прод-венв, живая БД; py_compile OK, md5 Mac==LXC101):
  (а) dmp/ct0084 ДО `raw='Faw Bestune T77'`/`brand='Faw Bestune T77'` → ПОСЛЕ `raw='Конкуренты'`/`brand=''`
  (ct0084 в структуре ещё нет → симуляция подстановкой в `_struct_names`, код и БД живые);
  (б) все 16 авто-слепков: `_struct_ct_names={}` → ct0084 = 'Faw Bestune T77' без изменений;
  (в) 72 резолва, изменилось 2 уникальных, оба с `brand ''→''` (контент не затронут, меняется только имя
  группы): ct0000 'полное отсутствие ключей'→'Ретаргетинг', ct0834 'Конкуренты'→'МК Конкуренты - КС'.
- Статус: 🟡 фикс в файлах (Mutagen→LXC101), ЖДЁТ рестарта direct-create/worker (на момент правки шла
  живая джоба porg-ozge4ntu — не прерывалась) + боевого прогона dmp после внесения ct0084 в структуру.
- НЕ помогло ранее: обход через выделенный ct0834 вне авто-пространства (2026-07-12) — рабочий обход,
  но не лечил механику: любой следующий ct не-авто слепка в авто-диапазоне повторил бы историю.
- Смежная находка — ✅ ПОЧИНЕНА 2026-07-18: `slepki_store._order()` глобил `*.json`, исключая
  ХАРДКОДОМ только `_order.json` → артефакт `slepki/_proposed_dmp.json` (`key:"dmp"`) становился
  18-м слепком, `assemble()` отдавал dmp дважды (выборку `next()` не ломало — первым шёл настоящий
  dmp.json, но любой залежавшийся `_proposed_*.json` молча уехал бы в прод, а review-first workflow
  прямо предписывает класть proposal рядом). Решение: `_is_part()` (`slepki_store.py:38`) — служебным
  считается ЛЮБОЕ имя с `_` в начале (покрывает `_order`, `_proposed_*`, `_gap_*` и будущие), фильтр
  применён и к глобу, и к списку из `_order.json`. Факт Mac и LXC101 (прод-венв): assemble 18→17,
  дублей нет, все 17 настоящих слепков на месте. Артефакт перенесён в `.claude/sdd/_proposed_dmp.json`.

### TP67_KEYWORDS_CROSS_SLEPOK_BLEED — КС-позиции брали ключи ЧУЖОГО директолога (2026-07-18)
- Симптом: КС-кампании tp6/tp7 одного слепка уезжали в кабинет с семантикой другого дилера.
  Факт: `terehov` tp7 «(Т|T)К - Общие запросы - КС - DM/CR/СR» (Мультибренд/Монобренд/С пробегом,
  8 позиций) брали набор `pavlov`; `zubakin`/`karavaev`/`salamahin`/`piterkina`/`scherbakova` —
  наборы `terehov`/`pavlov`. Замер по всем 15 слепкам: **349 tp6/tp7-позиций** с чужим донором,
  из них **54 в КС-режиме** (только они реально запрашивают ключи при создании).
- Где: `create_set_context._tp67_keywords_from_real_library._score` (:319-343), библиотека
  `tp67_real_keywords.json` (293 item'а, всего 5 слепков-доноров: pavlov 124, terehov 123,
  karavaev 32, dmp 9, scherbakova 5). Потребители — `create_set_master_product.py:124-128`
  (создание) и `slepki_editor.read_group_keywords:418` (карточка ключей в UI, фикс `18427c3`).
- Root-cause: `same_slepok` стоял ПЕРВЫМ элементом кортежа-ранга, но БЕЗ отсечения. Свой набор
  выигрывал, когда он есть; когда своего нет — молча выигрывал лучший ЧУЖОЙ по позиции/ct.
  Комментарий в коде это явно узаконивал («берём лучший реальный набор из другого слепка вместо
  падения "КС без ключей"»). Это тот самый открытый долг из `DMP_MK_KONKURENTY_AUTOTARGET`
  («жёсткий slepok-фильтр в `_score` для не-авто (анти-bleed)»).
- Решение (2026-07-18): в `_score` жёсткий фильтр `if it.get("slepok") != skey: return None` —
  чужой слепок источником быть НЕ может. Своих ключей нет → `([], [])`, и вызывающий
  (`create_set_master_product.py:130-153`) пишет «tp6/tp7 КС без ключей …» в `job["errors_log"]`
  + per-position `it_warnings`, а при явном `keyword_source` БЛОКИРУЕТ позицию. Текст ошибки
  расширен (позиция + «нет в паке и в СВОЕЙ библиотеке; заимствование запрещено»), чтобы
  деградация КС→autotarget была диагностируемой, а не тихой.
- Верифицировано (LXC101, прод-венв, реальный пак, харнесс по всем 608 tp6/tp7-позициям 15 слепков,
  ДО→ПОСЛЕ): чужой донор 349→**0** (КС 54→**0**). Цепочка tp7↦tp6 ЦЕЛА и работает внутри слепка:
  8 позиций terehov/tp7 переехали `pavlov` → `terehov` через tp6. Позиции, бравшие СВОИ ключи
  (111 КС), изменились у **0** — в т.ч. `dmp/tp6/ct0834` = 69 фраз (карточка UI: 69,
  `kw_source=real_library`). Харнесс сверен с реальной `_tp67_keywords_for`: 0 расхождений после
  фикса, 325 расхождений при старом (нефильтрованном) правиле — доказывает, что патч живой.
- ⚠️ ПОСЛЕДСТВИЕ (решение Семёна): **46 КС-позиций** остались БЕЗ ключей (zubakin 21, karavaev 14,
  piterkina 4, salamahin 3, scherbakova 3, pavlov 1) — раньше они молча несли чужую семантику.
  Теперь они деградируют в autotarget с явным warning в `errors_log`. Лечится ТОЛЬКО досбором
  собственных корпусов (M3-пак или харвест в `tp67_real_keywords.json`), а не кодом.
- Статус: 🟡 задеплоено для UI (`direct-slepki`/`direct-slepki-worker` перезапущены 21:45, active,
  журнал чист, карточка проверена). **`direct-create`/`direct-create-worker` НЕ перезапущены** —
  на момент фикса они выполняли живую джобу создания (заливка картинок porg-ozge4ntu). Создание
  подхватит фикс только после их рестарта.
- НЕ помогло бы: оставить ранжирование и добавить порог «чужой только если позиция совпала точно» —
  как раз точное совпадение позиции («общие запросы») и давало утечку pavlov→terehov.

### CT_NAMING_GAP_LADA_LARGUS_XRAY — ct0885/ct0890 не резолвились в имя (2026-07-18)
- Симптом: `chepelev` / Мультибренд / tp1, группы «Lada Largus» и «Lada Xray» (кодеры
  `ct0885_aon_n000_r0000_ct001_ag011_g00` / `ct0890_…`) → бренд `''` → нет бренд-контента и
  картинок, не строится mark-фильтр моделей.
- Где: `campaign_naming._ag_part1_map` (`public.gsheet_naming type='ag_part1'` 318 кодов +
  `public.leadgen_ct_naming`). Ни в одном источнике ct0885/ct0890 не было.
- Root-cause: коды-«двойники» авто-справочника со сдвигом +700 (ct0185 «Lada Largus» → ct0885,
  ct0190 «Lada Xray» → ct0890) заведены в структуре слепка, но не зарегистрированы в справочнике.
- Решение (2026-07-18, БД): `INSERT INTO public.leadgen_ct_naming` → `ct0885='Lada Largus'`,
  `ct0890='Lada Xray'` (формат 1:1 с авто-справочником ct0185/ct0190), 37→39 строк.
- Верифицировано (LXC101, прод-венв): ДО `_ag_part1_map()['ct0885']=None`,
  `_brand_ct_from_coder(ct0885)=('','')`; ПОСЛЕ `'Lada Largus'` и `('Lada Largus','ct0885')`
  (аналогично ct0890). Обратная карта НЕ сдвинулась: `_ct_for_name('Lada Largus')=ct0185`,
  `('Lada Xray')=ct0190` (в `_ag_part1_rev` gsheet идёт первым, `setdefault` не перезаписывает).
  `ct0834` (dmp «Конкуренты») не затронут: `('','')`, в бренд не течёт.
- Статус: 🟡 БД применена. `_AG1_NAME_CACHE` — на процесс: `direct-slepki` перезапущен и уже видит
  имена; `direct-create`/`direct-create-worker` подхватят на ближайшем рестарте (см. запись выше).

### PACK_MINUS_PER_GROUP_LOST — слепковые минуса tp5 терялись целиком + библиотечный набор в обход режима (2026-07-18)
- Симптом: в кампанию 712878290 (tp5, слепок `pavlov`, `porg-ozge4ntu`, job `633798f99dba`) доехала
  ОДНА минус-фраза «отзывы» (глобальная). Слепковые минуса tp5 не доехали НИ на группу, НИ на
  кампанию. Замер: `_collect_pack_minus("pavlov", <site_type>, "tp5")` = **0** при 977 у tp2, хотя
  в паке лежат 9 файлов `pavlov__aon_n000_ct009_ag011_g00_minus.txt` по 558 строк. Дополнительно:
  в интерфейсе на tp5-кампании висел набор «Минуса общие tp2», хотя у pavlov режим `campaign`.
- Где: `create_set_minus.py:_collect_pack_minus`; `create_set_feed_builders.py:_tp5_account_data`
  (потребители — tp5 `_create_tp5_single`, tp3 `_create_tp3_single`).
- Root-cause (три независимых дефекта):
  1. `_collect_pack_minus` читал только `ct_data["minus"]`. `kontent_pack.py:1562-1567` кладёт
     per-adgroup минуса в `ct_data["_groups"][gk]["minus"]`, а top-level `minus` у синтезированных
     ct заполняется ТОЛЬКО из `_minus_shared`. У pavlov `pavlov_minus_shared.txt` нет → tp5 = 0.
     Режим `campaign` снял групповые минуса → 558 фраз не доезжали никуда.
  2. Гейт `_SLEPOK_MINUS_MODE == "shared_set"` был на пути tp2/tp4 (`:435`), но НЕ на tp5/tp3 →
     слепку в режиме `campaign` всё равно цеплялся библиотечный набор в обход режима.
  3. Слепой фолбэк `next((mid ... "Минуса общие" in nm), msets[0][0])` — при отсутствии набора с
     нашим маркером брался ПЕРВЫЙ ПОПАВШИЙСЯ набор аккаунта. На кабинете директолога с
     собственными наборами это прицепило бы к нашей кампании ЧУЖОЙ набор и порезало показы.
- Решение (2026-07-18): (1) per-group минуса поднимаются на кампанию **ПЕРЕСЕЧЕНИЕМ** по всем
  носителям минусов tp (каждая группа + каждый легаси-ct без групп), а НЕ объединением; top-level
  `ct["minus"]` объединяется как раньше. (2) резолв набора в `_tp5_account_data` обёрнут в гейт
  `== "shared_set"` (закрывает и tp5, и tp3 — оба берут `data["minus_set"]`). (3) слепой фолбэк
  убран здесь и в `_get_or_create_minus_set` — без набора с маркером не цепляем ничего (на
  tp2/tp4-пути вместо этого создаётся СВОЙ набор, как и задумано докстрингом).
- Замер до→после (LXC101, прод-венв, реальный пак): pavlov tp5 **0 → 559** (все 3 типа сайта),
  pavlov tp2 **977 → 977** (без регресса и дублей). По 6 слепкам × 3 типа × tp1-tp5 сбор не
  уменьшился нигде, «до» ⊆ «после» везде. Гейт: pavlov/kryuchkova/terehov → `minus_set=None` и
  **0** запросов `negativekeywordsharedsets` (запросов к API стало меньше, не больше);
  scherbakova с нашим набором → id, только с чужими → `None`.
- ⚠️ Ключевое: ОБЪЕДИНЕНИЕ per-group минусов (первая, отвергнутая редакция) — ловушка. Замер:
  у kryuchkova 327 РАЗНЫХ per-group минус-файлов (дискриминаторы чужих моделей) → объединение
  заминусовало бы 56 938 СОБСТВЕННЫХ ключей из 60 435 (94%) на Мультибренд/tp5. Пересечение
  только по группам оставляло дыру на легаси-ct без групп (kryuchkova/Монобренд/tp5: 7 508 из
  18 290). Итоговое пересечение по группам И легаси-ct даёт прирост блокировок собственных
  ключей **0 везде** при сохранении цели (pavlov tp5 = 559). У pavlov все 9 per-group файлов
  идентичны — поэтому пересечение = полный список.
- Статус: 🟡 задеплоено не было (правка на Mac, деплой за Семёном), ждёт прогона. Верификация:
  live `negativeKeywords` tp5-кампании pavlov должны содержать паковые фразы (не только «отзывы»),
  и на tp5/tp3-кампании НЕ должно быть привязанного набора «Минуса общие».
- НЕ помогло ранее: — (первая правка этого класса). Смежные: `#9 SLEPOK_MINUS_MISSING_ONLY_GLOBAL`
  (закрывал только `_minus_shared`/ct-уровень, per-group слой не видел),
  `MINUS_WORDS_MISSING_TP5_TP3_PRODUCT` (глобальные слова, другой источник).
  ⚠️ Гипотеза «просто объединить per-group минуса» — ОПРОВЕРГНУТА замером (см. выше), не повторять.

### PARTIAL_CAMPAIGN_LEFT_ON_AD_CREATE_FAILURE — кампания остаётся полусозданной, когда падает шаг объявлений (2026-07-18)
- Симптом (класс): в аккаунте оседает кампания-огрызок — оболочка + группы (+ иногда часть
  объявлений), но состав неполный. Наружу либо `ok:True` (дефект молчит и уезжает в приёмку), либо
  ошибка БЕЗ уборки: следующий прогон плодит дубли имён, а `defer`-фолбэк на куку не срабатывает.
  Два конкретных места, найденные разбором: (а) tp3 `add_listing_ad` вызывался ВНЕ try → raise
  оставлял кампанию с cid + ShoppingAd без ListingAd; (б) token-путь tp2/tp4 удалял недоделанную
  кампанию только при пустых `adgroups`, а «группы есть, объявлений 0» проходило как успех.
- Где: `create_set_feed_builders.py:_create_tp3_single` (шаг ListingAd) и
  `create_set_feed_builders.py:_create_text_via_token` (гейт недозаполнения после
  `_build_text_from_pack`).
- Root-cause: гейт «недозаполнения» проверял ТОЛЬКО наличие групп и ошибку билдера. Состав кампании
  = группы + ключи + объявления; отсутствие последнего звена не считалось недозаполнением, а
  исключение на шаге объявлений не имело обработчика с уборкой.
- Решение (2026-07-18, `create_set_feed_builders.py`): единое правило «шаг объявлений упал ⇒
  кампания недозаполнена» — (а) `add_listing_ad` обёрнут в try/except с
  `_delete_partial_campaign` + `defer:True` (`#ФИКС-8`); (б) в гейт недозаполнения добавлено
  `not build.get("ads")`.
  ⚠️ **Условие применимости `not ads` (проверять при любом переносе гейта):** оно валидно
  ТОЛЬКО там, где TextAd — единственный тип объявлений. На token-пути tp2/tp4
  `_build_text_from_pack` зовётся без `feed_id`/`with_shopping` (дефолты 0/False) → блок товарных
  (`create_set_text_builders.py:231 if feed_id and with_shopping`) не выполняется, ключей
  `listing_ads`/`shopping_ads` в rep нет. На tp1/tp5 (`with_shopping=True`) пустой `ads` при живых
  товарных ЛЕГАЛЕН — туда этот гейт переносить НЕЛЬЗЯ, иначе снесёт валидную товарную кампанию.
- Статус: 🟡 задеплоено не было (правка на Mac, деплой за Семёном), ждёт прогона. Верификация:
  прогон tp2/tp4 + tp3 — при падении объявлений в аккаунте не должно оставаться кампании-огрызка,
  в отчёте джобы `partial_deleted:true` + `defer:true`; кампании с группами и 0 объявлений не
  должны приходить с `ok:true`.
- НЕ помогло ранее: — (первая правка этого класса). Смежные: `TP3_PARTIAL_SHOPPING_ONLY`-симптомы
  из прогонов Павлова; `NO_ADPRICE_LIVE` (гейт опирался на ключи `shopping_ads`/`listing_ads`,
  которых в live-counts нет — тот же корень «гейт по неверному ключу состава»).

### VERIFY_BLIND_TO_CAMPAIGN_ASSETS — верификатор СОЗДАНИЯ не видел уточнения / набор быстрых ссылок / промо (2026-07-18)
- Симптом (класс, не единичный баг): дефекты кампанийных ассетов ловились ТОЛЬКО руками, верификатор
  создания молчал. Реальные инциденты: `DMP_CALLOUTS_NOT_PUSHED` (пилот porg-mushirne, 0/14 кампаний
  без callouts — нашли руками), `CALLOUTS_NAMEERROR_TIME` (ошибка глушилась, кампании молча без
  уточнений), `CALLOUTS_NOT_CREATED`, `SITELINK_SET_NULL_SILENT` («sitelink_set_id=null, tp1 РСЯ без
  быстрых ссылок, в отчёте ни ошибки, ни причины»), `SITELINKS_ONLY_1_OF_8`, `DMP_SITELINKS_AUTO_BLEED`.
- Где: `grid_content_verifier.py` — уточнений и набора быстрых ссылок не проверял ВООБЩЕ (sitelinks
  проверялись только на уровне ОБЪЯВЛЕНИЙ). `PROMO_MISSING` был **мёртв дважды**: (а) `live_verifier.py:114`
  звал `verify_grid_content(nm, cid, counts)` БЕЗ `expected` → `exp_promo=None` → `bool(exp_promo)`
  никогда не истинно; (б) `grid_read.py:150,175` — `promoExtensionId` убран как `FieldUndefined`, поле
  всегда None.
- Root-cause: `read_campaign_invariants` (`grid_finalize.py`) уже делала запрос `CampaignsEditData`,
  строка которого СОДЕРЖИТ `inheritableCallouts` / `inheritableSitelinkSet` / `promoExtension`
  (фрагмент `UnifiedCampaign` в `grid_campaigns_edit_data.graphql`), но извлекала ~9 полей и
  остальное ВЫБРАСЫВАЛА.
- Решение (2026-07-18): три поля извлекаются из ТОГО ЖЕ ответа → **0 новых HTTP-запросов, 0 баллов**.
  Новые коды `CALLOUTS_MISSING_LIVE` / `SITELINK_SET_MISSING_LIVE` + оживлён `PROMO_MISSING`
  (все warn, report-only — `set_campaign_invariants` эти поля не переставляет, ремонт не выдумываем).
  Гейты: только tp1–tp5 (**tp6/tp7 уточнения не поддерживают** — не флагаем); tri-state (ключ не
  пришёл → None → тишина). Промо ДВУХСТУПЕНЧАТО (требование Семёна): сначала выясняем, есть ли
  промо в АККАУНТЕ вообще; аккаунт без промо → код не выдаётся вообще.
- ⚠️ Доработка (2026-07-18, по ревью): ступень 1 сначала считалась ПРОКСИ по кампаниям набора
  (`live_verifier._account_has_promo`) — и имела слепой угол ровно в главном сценарии: если промо
  не доехало НИ ДО ОДНОЙ кампании (**0/N**), прокси возвращал `False` и код молчал; ловился только
  частичный провал M/N. Починено пробросом НАСТОЯЩЕГО признака библиотеки: v5 `promotions.get`
  **уже вызывается** в штатном потоке (`create_set_promo.py:34`, `precreate.py:262`), поэтому
  `attach_or_create_promo` теперь возвращает `(note, bool(promos_all))`, и признак идёт
  orchestrator → `_create_set_live_verification` → `verify_create_set_live` →
  `verify_live_create_set(account_has_promo_library=…)`. **0 новых HTTP-запросов, 0 баллов.**
  Прокси оставлен ФОЛБЭКОМ на `None` (вызов не из потока создания) — прежнее поведение цело.
- ⚠️ Грабля (не повторять): нормализацию брать из `_unified_campaign_update_from_edit_row:601-603`,
  а НЕ читать сырой rowset по write-именам (`calloutIds`/`sitelinkSetId`/`promoExtensionId`) — в сыром
  ответе они под `assetValue` / `promoExtension.id`, чтение «по write-ключу» даёт вечный None
  (ровно так и умер прежний `PROMO_MISSING`).
- Статус: 🟡 код + юниты, ждёт живого прогона создания. py_compile OK (Mac + прод-венв LXC101);
  30 юнит-кейсов верификатора + 16 кейсов парсинга/гейта + 6 E2E — все зелёные; счётчик Grid-операций
  `campaign_content_counts` до/после = **10/10** (идентичный набор операций). После доработки:
  ещё **36 + 20** кейсов (в т.ч. 0/N, M/N, пустая библиотека, фолбэк `None`) — зелёные на Mac и
  LXC101; baseline-сверка вызовов до/после = **1/1** v5 `promotions.get` и **6/6** Grid-операций.
- НЕ помогло ранее: — (первая реализация; прежний `PROMO_MISSING` не «не помог», а был мёртв —
  причина выше).

### RETRY_ON_NETWORK_LOSS_DUPLICATES_ADD — ретрай транзиента мог продублировать add-чанк (2026-07-18)
- Симптом: потенциальный (найден ревью, не наблюдался live). Ретрай `bounded_post` не различал
  источник ошибки: обрыв связи (`ConnectionResetError` → маркер `connection`) и hard timeout тоже
  считались транзиентом → повтор `campaigns.add`/`adgroups.add`/`ads.add`/`keywords.add` при том,
  что сервер мог УЖЕ применить запрос и не успеть ответить. Чанк на 96 групп = 96 лишних объявлений.
- Где: `yandex_gateway.bounded_post` (ретрай-цикл), маркеры `_TRANSIENT_MARKERS`.
- Решение (2026-07-18): различаем ДВА источника. Ответ Директа с верхнеуровневым `error` = отказ
  запроса целиком → повтор безопасен для любого метода (ради этого Д2 и делался — сохранён).
  Локальный сбой (exception / hard timeout, ответа нет) → повторяем только идемпотентные методы;
  `_creates_objects(body)` (метод начинается на `add`, либо метод не распознан) → возврат без
  ретрая. `get/update/delete/suspend/resume/archive` идемпотентны по Id — ретраятся в обоих случаях.
  Валидация (4000, DefectIds) не ретраится нигде — маркеров не содержит.
- Статус: 🟡 фикс на Mac, 22/22 офлайн-кейса (мок транспорта) OK, ждёт деплоя + живого прогона.
- НЕ помогло ранее: первая редакция Д2 (коммит `4c75cfb`) ретраила ВСЁ транзиентное включая
  сетевые обрывы на `add` — docstring утверждал безопасность, которой код не давал.

### FOREIGN_MODEL_FILTER_EMPTIES_ADGROUP — per-adgroup группы уезжали с 0 ключей (2026-07-18)
- Симптом: `NO_KEYWORDS_LIVE` на 4 tp2-кампаниях porg-ozge4ntu (слепок pavlov, Мультибренд, job
  194c27f8c9b5); реально пусты 8 групп (2 из 35 «Марка», 6 из 150 «Модели»).
- Где: `text_gen._filter_group_keywords` (фильтр чужих моделей + анти-пустой гейт),
  колл-сайты `create_set_text_builders.py:475`, `create_set_tp1_builders.py:904`.
- Root-cause (3 слоя): (1) фильтр чужих моделей строился по **ct-уровневому** имени («Lada Granta»),
  а группы теперь per-adgroup (gk) — под-модели одного ct; disc содержал `лифтбек/седан/универсал/
  хэтчбек/sw/cross` → у группы `lada_granta_liftback` ВСЕ ключи содержали свой же кузов и дропались.
  (2) кузов в структуре латиницей («Lada Granta Liftback»), в `brand_models_catalog.json` кириллицей
  («Granta Лифтбек») — own-токены не гасили свой же дискриминатор. (3) анти-пустой фолбэк не спасал:
  в списке оставался спецключ `---autotargeting` → `or kws` считал список непустым, а
  `grid_create.add_keywords` (grid_create.py:230) спецключ при заливке пропускает → в кабинете 0.
  Плюс минус-части фразы («лада гранта лифтбек **-спорт**») считались признаком чужой модели.
- Решение (2026-07-18): `text_gen.py` — `_MODEL_TOKEN_ALIASES` + `_expand_model_tokens` (кир↔лат сшивка
  кузова), `_kw_positive_tokens` (минус-части фразы не участвуют в дискриминации), `_real_kw_count`
  (анти-пустой гейт по РЕАЛЬНЫМ ключам, во всех трёх сегментах); колл-сайты передают модель САМОЙ
  ГРУППЫ `model=(_uname if (_multi and _uname) else brand)`.
- Офлайн-прогон на реальных паках pavlov (было→стало, реальных ключей):
  lada_granta_liftback 0→13, lada_granta_sedan 0→3, lada_granta_universal 0→9,
  lada_iskra_sw_cross 0→1, chery_tiggo_4_pro_18_years 0→15. Протечки чужого кузова 0;
  регресс-кейсы FOREIGN_MODEL_KEYWORDS_IN_MODEL_GROUP (cs75/цс75/uni-k → drop, cs35plus/cs35 → keep) — OK.
- Статус: 🟡 фикс на Mac, py_compile OK, ждёт деплоя + живого прогона.
- НЕ помогло ранее: —

### COPY_STEP_KEYWORDS_TEXT_AD_GROUP_GRID_LIES — Grid addKeywords для TextAdGroup возвращает ложный success, ключи не персистятся (2026-07-18)
- Симптом: tp2 поисковые кампании в копии получают 1 ключ на группу вместо ~96. `step_keywords`
  рапортует `via_grid=N` (полный успех), `via_v5=0`, `fail=0` — но v5 `keywords.get` показывает 1/группа.
  Воспроизведено: porg-lzjk6p5m tp2, run-15 с уже задеплоенным `n_added`-фиксом → та же картина.
  tp1 РСЯ работает корректно.
- Где: `copy_steps.py:step_keywords` — маршрутизация в Grid (было без проверки типа группы).
- Root-cause: `dc.phase_upload` создаёт tp2 target-группы через v5 `adgroups.add` как **TEXT_AD_GROUP**
  (старый формат). Grid `addKeywords` для TextAdGroup возвращает NON-пустой `addedItems`
  (`n_added==len(batch)`, ложный success), при этом ключи в Яндекс-бэкенде НЕ персистятся.
  Для ЕПК-групп (UNIFIED_AD_GROUP, tp1 RSYa) Grid работает корректно.
- Решение (2026-07-18): `copy_steps.py:841-867,907-922,976-980` — тип-детектор из
  `src_dir/adgroups.json` (phase_pull), новая ветка `elif Type=="TEXT_AD_GROUP"` в цикле routing:
  эти ключи идут в `v5_text_rows` (v5 keywords.add, агентские баллы) напрямую, минуя Grid.
  Новый счётчик `rep["v5_text_adgroup"]`. Fallback: если adgroups.json нет/ошибка — все ключи
  остаются в Grid (прежнее поведение, без регресса). UNIFIED_AD_GROUP (ЕПК) → Grid без изменений.
- ⚠️ Существующие копии: `keywords_done.json` отравлен (все ключи помечены done старым кодом).
  Для repair стереть keywords_done.json + перегнать step_keywords вручную.
- Статус: 🟡 py_compile + pyflakes OK (Mac), ждёт живого прогона.
- НЕ помогло ранее: (1) фикс `n_added = len(added or [])` (без `or len(rows_b)`) — Grid всё равно
  врёт non-пустым addedItems, via_v5=0 после фикса.

### MOSCVICH_KEYS_DROPPED_AS_FOREIGN_CITY — марка «Москвич» съедается фильтром чужих городов (2026-07-18)
- Симптом: группа `moscvich_общее` (ct0252, tp2 «Марка», pavlov) уезжает с 0 ключей — 19/19 ключей
  дропаются ещё ДО марочных фильтров.
- Где: `city_morph._drop_foreign_city_keywords` (:168), стем `"москв"` в `_RU_CITY_STEMS`.
- Root-cause: `re.search(r"\bмоскв", "купить машину москвич")` матчит МАРКУ «москвич» как город
  «Москва» → ключ считается чужегородним. Диагностика прогона 194c27f8c9b5 приписывала эту группу
  фильтру чужих моделей — по факту причина другая (доказано пошаговым прогоном пайплайна).
- Решение: `city_morph.py:168-180` — `_NON_CITY_STEMS = ("москвич",)` + хелпер `_city_stem_hit()`.
  Матч городского стема теперь проверяется на уровне СЛОВА: берём слово целиком от позиции
  совпадения и отбрасываем его, если оно начинается с не-городского стема (более длинный/
  специфичный префикс перебивает городской). «москвич»/«москвича»/«москвич-412» → марка (KEEP),
  «москве»/«москвы»/«москва» → город (DROP). Механизм общий: новая коллизия = +1 строка в кортеж.
  Тот же принцип, что уже применён в `geo_strip.py:158` (точные формы + коммент про Москвич).
- Статус: 🟡 ждёт live-прогона. Локально доказано на LXC101 (old-vs-new на реальных паках):
  позитив — pavlov `ct0252/moscvich_общее` 0/19 → 19/19 выжило, `ct0253/moskvich_3` 0/12 → 12/12;
  регресс гео — «москва»/«москве»/«москвы»/«лада веста москва»/«kia волгоград» по-прежнему DROP,
  «москвич 3 в москве» DROP (в ключе реальный город); регресс по 6 слепкам (pavlov, kuderko,
  chepelev, gordeeva, salamahin, terehov) × 5 городов, ~410 тыс. ключей: `-new=0` (новая НЕ
  дропнула НИЧЕГО, что держала старая) и вся дельта +new состоит ТОЛЬКО из ключей «москвич»
  (не-москвич в дельте = 0). При own_city=Москва дельта 0 (стем не в foreign) — как и было.
- Пред-существующий пробел (НЕ регресс правки, был и до неё): «московский»/«подмосковье» стем
  «москв» не ловит вовсе («моск-о-вский» ≠ «москв») — old и new одинаково KEEP. Отдельная задача.
- НЕ помогло ранее: —

### UAC_FULL_PATCH_WIPES_CONTENTS — PATCH любого поля МК обнулял картинки (2026-07-17)
- Симптом: PATCH поля `socdem` у Мастер-кампании обнулил её картинки в 0 (живые МК porg-r7ro6tei,
  пришлось перезаливать 45 картинок).
- Где: `routes_content_editor._uac_campaign_patch_payload` (:601) + whitelist `_UAC_PATCH_FULL_KEYS` (:587).
- Root-cause: тот же класс, что `GRID_RMW_DISPLAY_HREF_WIPED` — full PATCH = REPLACE всего тела; ключа
  креативов не было в whitelist → уходил из запроса → UAC трактовал как пустое. **Асимметрия имён:**
  detail отдаёт креативы как `contents` (list[dict] id/type/direct_image_hash), а save ждёт
  `content_ids` (list[str]) — добавить `"content_ids"` в whitelist БЕСПОЛЕЗНО (строка 603 фильтрует
  `if k in detail`, а такого ключа в detail НЕТ). Партиал-PATCH `socdem` даёт HTTP 500 → full-путь всегда.
- Решение: `routes_content_editor.py:603-610` — деривация `payload["content_ids"] = [c["id"] for c in
  detail["contents"]]` перед `payload[field_key] = values` (патч самого `content_ids` не затирается —
  values ставятся после).
- Статус: ✅ подтверждено live 17.07.2026 (porg-r7ro6tei, МК 712850047, черновик): partial socdem = HTTP 500
  (доказано, что идёт full-путь); full PATCH через патченый билдер → картинок 5→5, хеши идентичны;
  реальная запись применяется (age_18→age_25→age_18, картинки целы, socdem возвращён 1:1). НЕ задеплоено.
- НЕ помогло ранее: — (первая правка). Гипотеза «добавить `content_ids` в `_UAC_PATCH_FULL_KEYS`» —
  ОПРОВЕРГНУТА зондом (ключа нет в detail → no-op); не повторять.
- ⚠️ Дополнение 2026-07-18 (сверка билдера с HAR `direct.yandex.ru.61har.har`, PATCH-entries 128/248,
  оба 200, браузер шлёт 33 ключа). Прогон РЕАЛЬНОГО `_uac_campaign_patch_payload` на РЕАЛЬНОМ detail
  из HAR: деривация `content_ids` работает 1:1 с браузером (порядок и состав совпали) — фикс выше
  подтверждён ещё и офлайн. Вскрылись ДВА пропущенных ключа того же класса:
  1. **`reserve_landing_id`** — в detail ЕСТЬ (значение `None`), в whitelist не было → уходил из
     запроса. ЗАКРЫТО: добавлен в `_UAC_PATCH_FULL_KEYS` (`routes_content_editor.py:598`); проходит
     обычным `if k in detail`, шлётся `None` — 1:1 с браузером.
  2. **`relevance_match` (автотаргетинг) — НЕ ЗАКРЫТ, открытый риск того же класса.** Ключ в
     whitelist ЕСТЬ, но в detail его НЕТ: там `relevance_match_categories` (третья асимметрия имён
     после `contents`/`content_ids`) → `if k in detail` его выбрасывает → full PATCH шлётся БЕЗ
     автотаргетинг-категорий. Деривация НЕ сделана СОЗНАТЕЛЬНО: составы не совпадают —
     detail даёт `EXACT_V2_MARK`/`NARROW_MARK`, браузер шлёт `EXACT_MARK`/`COMPETITOR_MARK`, плюс в
     detail есть `brand_settings`, которых в write-форме нет. Механика перевода НЕ ДОКАЗАНА, а
     угаданная деривация ЗАПИШЕТ НЕВЕРНЫЙ таргетинг (хуже, чем пропуск). ⚠️ Нужен зонд на черновике:
     full PATCH → сравнить категории автотаргетинга ДО/ПОСЛЕ. Не гадать по именам enum'ов.
  Статус дополнения: 🟡 `reserve_landing_id` — код + офлайн-сверка с HAR, живого PATCH НЕ было;
  `relevance_match` — только диагностика, правки нет.
- ⚠️ Дополнение 2026-07-19 (зонд `porg-gcegsszl`, МК 708193487, сверка с `_har/UAC_image_replace.json`
  — HAR ровно этой кампании, step 3 = замена картинки на позиции 5). **Мутаций 0: зонд остановлен
  пред-полётной сверкой, full PATCH НЕ отправлялся.** Прогон реального `_uac_campaign_patch_payload`
  на реальном detail: ключей 33 против 33 у браузера — но **состав РАЗНЫЙ** (совпадение счёта обманчиво):
  1. **`relevance_match` — риск ПОДТВЕРЖДЁН как материальный, не теоретический.** Кампания
     `MK_AT_...` (AT = автотаргетинг), в detail `relevance_match_categories.active=true` с
     selected-категориями (ALTERNATIVE_MARK/BROADER_MARK/ACCESSORY_MARK…). Ключ выбрасывается
     `if k in detail` → full PATCH = REPLACE обнулил бы автотаргетинг кампании, смысл которой —
     автотаргетинг. Write-форму из этого HAR вывести НЕЛЬЗЯ: значение вырезано
     (`"<omitted…, 119 chars in source>"`). Гадать по-прежнему запрещено.
  2. **`ecom` — НОВЫЙ пропущенный ключ того же класса.** `routes_content_editor.py:629-632`: при
     `keywords is None` (у этой МК именно так) билдер попает `ecom` вместе с feed-полями. Комментарий
     «browser … omits feed fields» верен ТОЛЬКО про feed-поля — **`ecom` браузер шлёт** (`ecom:false`
     в step 3, в detail тоже `false`). Попается сверх необходимого.
  3. Лишние против браузера: `field_to_use_as_body`, `field_to_use_as_name`. ⚠️ **ИСПРАВЛЕНО
     2026-07-19: «браузер их не шлёт» НЕВЕРНО** — браузер шлёт их явным `null`, ключ опускаем МЫ
     (см. п.2 дополнения ниже).
  4. **Деривация `content_ids` — ЗЕЛЁНАЯ** на живом detail: все 5 id, позиция 5 заменена, порядок
     остальных сохранён, 1:1 с браузером. Фикс 2026-07-17 держится.
  5. ⚠️ **Нет точки прерывания между партиалом и full.** `_uac_patch_campaign_texts` (:636-662) —
     один вызов `try: partial → except: build full → send`. «Позвать только партиал» через прод-путь
     невозможно → любой вызов = согласие на возможную отправку неполного full-тела по живой кампании.
     Поэтому «попробовать и посмотреть» НЕ является безопасной стратегией; не повторять как идею.
  Статус дополнения: 🟡 `relevance_match` — остаётся ОТКРЫТЫМ (нужен неурезанный HAR либо зонд на
  расходной МК: full PATCH → сравнить категории ДО/ПОСЛЕ). `ecom` — найден, правки НЕТ.
- ⚠️ Дополнение 2026-07-19 (правка по пп. 2-3 предыдущего дополнения; мутаций 0, только офлайн-сборка).
  1. **`ecom` — ЗАКРЫТО** (`routes_content_editor.py:626-635`): убран из pop-списка ветки
     `keywords is None`, там остались только собственно feed-поля (`feed_id`, `listings_feed_id`,
     `feed_filters`, `listings_feed_filters`). В pop добавлен guard `key != field_key` (не выбрасывать
     патчимое поле). Факт: `ecom=False` в payload, у браузера `False`.
  2. **`field_to_use_as_body`/`field_to_use_as_name` — ЗАКРЫТО без риска для товарки**
     (`:636-641`): из whitelist НЕ убраны, выбрасываются из payload только при значении `None`.
     Обоснование: под REPLACE-семантикой выброс None-ключа эквивалентен записи пустого → терять
     нечего; у tp7 там непустые имена полей фида → сохраняются. Симуляция tp7 подтвердила: оба поля
     остаются. Слепое удаление из whitelist НЕ применялось (сломало бы товарку).
     ⚠️ **ИСПРАВЛЕНО 2026-07-19 проверяющим: обоснование опиралось на несуществующий факт.**
     Было записано «у обычной МК оба `None` — браузер их и не шлёт». Опровергнуто **8 эталонами**
     HAR-корпуса: браузер шлёт ключ явным `null`, опускаем его МЫ.
     ```
     field_to_use_as_body: browser=None | ours=ABSENT | detail=None
       (31/32/33/34har e50, 34har e701, 55har e193, 59har e138, 60har e205)
     ```
     Под REPLACE выброс ключа ≈ отправка `null`, поэтому поведение считаем эквивалентным и код
     НЕ меняем — но это вывод из семантики REPLACE, **живьём не проверено**. Если когда-нибудь
     всплывёт, что сервер различает «ключ отсутствует» и «ключ = null», начинать надо отсюда.
  3. Итог сверки состава с эталоном на реальном detail из снимка ДО. ⚠️ **Числа ИСПРАВЛЕНЫ
     2026-07-19 (раунд 2), в первой редакции были неверны** («ДО 33 / MISSING 2» — не измерялось
     от HEAD). Перемерено от чистого HEAD (`1bfb7e0`) на detail `probe-porg-gcegsszl-before.json`
     против эталона HAR-64 entry 611 (33 ключа):
     * **ДО (HEAD): 32 ключа**, MISSING `ecom`, `relevance_match`, `reserve_landing_id`;
       EXTRA `field_to_use_as_body`, `field_to_use_as_name`. Т.е. `reserve_landing_id` в HEAD
       НЕ было — он добавлен этим же раундом (правка whitelist в отчёте раунда 1 не заявлена).
     * **ПОСЛЕ раунда 1: 32 ключа**, MISSING `relevance_match`, EXTRA пусто.
     Деривация `content_ids` зелёная (5/5, порядок 1:1), сценарий патча `texts` не сломан.
     py_compile+pyflakes чисто.
  4. Пред-существующие расхождения ЗНАЧЕНИЙ (не состава), НЕ чинились: `minus_regions` `None` vs `[]`,
     `week_limit` float vs строка, лишние подключи `socdem.income_*` и `goals.goal_template_type`.
  Статус дополнения: 🟡 ждёт прогона — код есть, живого PATCH не было, не задеплоено.
  `relevance_match` по-прежнему ОТКРЫТ и правкой не затронут. Отчёт:
  `.claude/sdd/images-tab-uac-payload-report.md`.
- ⚠️ Дополнение 2026-07-19 (раунд 2, мутаций 0). Семён прислал НЕурезанные HAR:
  `~/Downloads/direct.yandex.ru.64har.har` entry **611** и `…65har.har` entry **755** — оба
  `PATCH /web-api/uac/campaign/707934116`, оба 200, 33 и 34 ключа. Write-форма `relevance_match`
  наконец видна целиком.
  1. **`relevance_match` — ЗАКРЫТО** (`routes_content_editor.py:635-651`): деривация из
     `detail["relevance_match_categories"]`. **Трансляция имён НЕ нужна** — entry 755 шлёт ровно
     read-имена (`EXACT_V2_MARK`, `NARROW_MARK`), совпадающие с detail 1:1. Форма записи —
     ПЛОСКИЕ списки: `{"active": <bool>, "categories": [<...где selected>],
     "brand_settings": [<...где selected>]}`. Берём только `selected:true`, ничего не достраивая;
     `disabled` НЕ фильтрует (это про доступность переключателя в UI, а не про включённость).
     `brand_settings` кладём только если непусто.
     ⚠️ Старая запись «браузер `brand_settings` не шлёт **никогда**» ошибочна — она сделана по
     одному PATCH (entry 611, легаси-набор `COMPETITOR_MARK`/`EXACT_MARK` без `brand_settings`).
     Точная формулировка (сверка по 16 эталонам, 2026-07-19): браузер шлёт **две живые формы** —
     read-форму с `brand_settings` (e755) и легаси-набор без него (9 из 16), сервер принимает обе.
     Мы шлём read-форму. Подробности и цифры — в п.6 дополнения «раунд 3» ниже.
     Соответствие легаси-имён read-именам (`COMPETITOR_MARK`↔`NARROW_MARK`, `EXACT_MARK`↔
     `EXACT_V2_MARK`) — ГИПОТЕЗА из совпадения множеств, в коде НЕ используется и использоваться
     не должна. В МК категории автотаргетинга в интерфейсе не выбираются (Семён) → набор всегда
     полный; частичный набор код отправит как есть, без изобретения маппинга.
  2. **Пустые feed-поля выбрасываются ВСЕГДА** (`:665-672`), а не только в ветке `keywords is None`:
     у МК 707934116 `keywords == []` (не `None`) → ветка не срабатывала и в payload уезжали 4
     лишних `null` (`feed_id`, `listings_feed_id`, `feed_filters`, `listings_feed_filters`),
     которых браузер не шлёт. Аргумент безопасности прежний: под REPLACE выброс None-ключа
     обнулять нечего; непустой фид tp7 не трогается (симуляция подтверждает).
  3. **Итог сверки состава: 33 против 33, MISSING [] EXTRA []** — на ДВУХ разных detail
     (`probe-porg-gcegsszl-before.json` 708193487 и detail из ответа entry 611, 707934116)
     против эталона entry 611.
  4. 🔴 **НОВЫЙ открытый риск того же класса: `ca_retargeting_condition`.** В entry 755 браузер
     шлёт его 34-м ключом; в нашем whitelist его НЕТ → full PATCH обнулил бы условие
     ретаргетинга у МК, где оно задано. В entry 611 у той же кампании оно было `null` и браузер
     его не слал — поэтому по одному HAR ключ не был виден. Асимметрия read/write: detail даёт
     `condition_rules[].goals[]` богатыми объектами (`id` числом + `name`/`type`/`description`),
     write-форма — только `{"id": "<строка>"}`. Деривация НЕ сделана: один сэмпл, вложенная
     структура, отдельное решение. **Не патчить МК с непустым `ca_retargeting_condition` через
     full-путь, пока не закрыто.**
  5. Расхождения ЗНАЧЕНИЙ из прошлого дополнения (`minus_regions`, `week_limit`, `socdem.income_*`,
     `goals.goal_template_type`) остаются, не чинились.
  Статус дополнения: 🟡 ждёт живого прогона — код + офлайн-сверка с двумя HAR, PATCH не слался,
  не задеплоено. Отчёт: `.claude/sdd/images-tab-uac-payload-report.md` (раздел «Раунд 2»).
- ⚠️ Дополнение 2026-07-19 (раунд 3, мутаций 0). **`ca_retargeting_condition` — ЗАКРЫТО**
  (`routes_content_editor.py:651-687`, деривация read→write перед `payload[field_key] = values`).
  1. Эталоны: HAR-65 entry 755 (МК 707934116, 34 ключа, ca ЕСТЬ) vs entry 611 (та же МК, 33 ключа,
     значение `null` → браузер ключ НЕ шлёт) + **второй независимый сэмпл** HAR-6 entry 16
     (МК 710852886, аккаунт `e-20086660`, 33 ключа, 10 целей) — write-форма в обоих идентична
     по устройству, т.е. выведена не по одному наблюдению.
  2. Форма: read `condition_rules[].goals[]` = богатые объекты (`id` ЧИСЛОМ + `name`/`type`/
     `description`/`segmentInfo`/`time`/`platformId`/`bundleId`); write = только `{"id": "<строка>"}`.
     Обёртка `condition_rules[].type`/`interestType` переносится как есть; верхнеуровневые
     `name`/`id` (в detail оба `null`) браузер не шлёт — не добавляем.
  3. **Источник — `ca_retargeting_condition`** (имя совпадает с write-ключом → соответствие прямое,
     не выведенное). Дубль `retargeting_condition` сверен по ВСЕМ 20 detail из HAR: содержимое
     совпадает 1:1 (0 расхождений), ключи всегда присутствуют парой → используется только как
     фолбэк при отсутствии ca-ключа, а не как источник.
  4. **Пусто → ключ не шлём вовсе** (повтор поведения браузера). Проверено на трёх пустых формах
     (`{}`, `condition_rules: []`, правило с `goals: []`) — ключ не появляется. Отправка пустой
     структуры под REPLACE = ОЧИСТКА условия, поэтому «на всякий случай слать» ЗАПРЕЩЕНО.
  5. Сверка состава с эталоном: **34 против 34, MISSING [] EXTRA []** (кампания С условием) и
     **33 против 33, MISSING [] EXTRA []** (та же кампания БЕЗ условия), плюс 33/33 на HAR-6/16.
     Значение `ca_retargeting_condition` побайтово равно браузерному во всех трёх. Регрессии нет:
     `content_ids` 5/5 с сохранением порядка, feed-поля tp7 (`keywords: []`) не выброшены.
     py_compile + pyflakes чисто.
  6. ⚠️ **ИСПРАВЛЕНО 2026-07-19 проверяющим: формулировка «отличается только ПОРЯДКОМ» была
     НЕВЕРНА.** Сверка со ВСЕМИ 16 браузерными PATCH-200 в HAR-корпусе (не с 1-3 эталонами)
     дала расхождение СОСТАВА в 9 из 16:
     ```
     ours_cats   = [ACCESSORY, ALTERNATIVE, BROADER, EXACT_V2_MARK, NARROW_MARK]
     ref_cats    = [ACCESSORY, ALTERNATIVE, BROADER, COMPETITOR_MARK, EXACT_MARK]
     ours_brands = [WITHOUT_BRAND, WITH_BRAND, WITH_COMPETITOR_BRAND]   ref_brands = []
     ```
     То есть в части эталонов браузер шлёт **легаси-набор категорий и `brand_settings` не шлёт
     вовсе**. Версия «легаси = старый браузер» ОПРОВЕРГНУТА хронологией: `65har` содержит ОБЕ формы
     для ОДНОЙ кампании 707934116 (e611 легаси, e755 v2, обе 200), а `61har` (2026-07-18) и
     `64har` (2026-07-19) тоже легаси. Значит это **два живых варианта write-схемы, сервер
     принимает обе**. Наш код шлёт read-форму (по e755, где write == read дословно) и
     `brand_settings` не вычищает — выбор консервативный, менять его без live-доказательства
     не нужно. Не верифицировано живым PATCH.
  Статус дополнения: 🟡 **ЧАСТИЧНО подтверждено живьём 2026-07-19, ОБЩЕЕ «✅» ОТКАЧЕНО.**
  Подтверждено ровно то, что перечислено ниже и что реально сверялось: `ca_retargeting_condition`,
  `relevance_match_categories`+`brand_settings`, `contents`, `typedCreatives`. **НЕ подтверждено —
  сохранность кампании и её объявлений в целом:** сверка шла по **28 полям из 127** и селектом уже,
  чем снимок ДО, а полная сверка 127 ключей вскрыла изменения ВНЕ отправляемых ключей
  (`UAC_FULL_PATCH_SIDE_EFFECTS_OUTSIDE_PAYLOAD` ниже). Читать этот блок как «асимметрии №5/№6
  на записи не обнуляются», а НЕ как «full PATCH безопасен».
  (probe 3, `porg-pvrbl7mh`, МК
  `712714472`: cost=0, 5 целей ретаргетинга, 5 контентов; прод-путь `run_image_replace` → UAC
  full PATCH 34 ключа). Пред-полётная сверка билдера с эталоном entry 755 на ЖИВОМ detail:
  **34/34, MISSING [] EXTRA []** (и та же картина на второй ретаргетинговой МК `712714457`) —
  стоп-условие не сработало, отправка разрешена. Независимый read-back по 28 полям кампании:
  **`ca_retargeting_condition` идентично ДО** (все 5 целей) и **`relevance_match_categories`
  идентично ДО** — т.е. обе асимметрии (№5 автотаргетинг, №6 ретаргетинг) на живой записи
  НЕ обнуляются. Заодно снят п.6 выше: **v2-форма `categories`+`brand_settings` сервером принята
  и настройку сохранила** — консервативный выбор оказался верным, менять не нужно.
  `contents`: заменена ровно позиция 3 из 5, позиции 0/1/3/4 целы; `typedCreatives` зеркального
  адаптивного объявления — 2 видео до и после. Откат вернул все 5 contents к снимку ДО.
  ⚠️ Единственное расхождение **в тех 28 полях** — **`device_types` меняет ПОРЯДОК** на каждом full
  PATCH (`[desktop,tablet,phone]` → `[phone,tablet,desktop]` → `[tablet,phone,desktop]`) при
  идентичном МНОЖЕСТВЕ значений; 4 последовательных чтения БЕЗ записи стабильны → порядок
  пересобирает сервер именно на записи, поле хранится как множество. Здесь потери таргетинга нет,
  правки не требует; diff-сверщик UAC-кампаний обязан сравнивать `device_types` как МНОЖЕСТВО,
  иначе даст ложное расхождение.
  ⛔ **Прежний вывод «это просто косметическая проблема diff-сверщиков» ОШИБОЧЕН и снят
  (2026-07-19).** Механика описана верно (сервер трогает поля вне payload), но обобщение было
  неверным: ТОТ ЖЕ механизм породил два НЕкосметических изменения — `inheritableCallouts`
  объявления-зеркала `CLEAR`→`INHERIT` и `organic_search_enabled` кампании `null`→`true`
  (см. `UAC_FULL_PATCH_SIDE_EFFECTS_OUTSIDE_PAYLOAD`). Косметичен КОНКРЕТНО `device_types`,
  а не класс «сервер переписал поле вне payload». Отчёт: `.claude/sdd/probe3-pvrbl7mh-report.md`;
  прежний офлайн-раунд — `.claude/sdd/images-tab-uac-payload-report.md` («Раунд 3»).

### UAC_FULL_PATCH_SIDE_EFFECTS_OUTSIDE_PAYLOAD — full PATCH меняет поля, которых НЕТ в payload (2026-07-19)
- Симптом: после UAC full PATCH кампании часть состояния кампании И зеркального адаптивного
  объявления отличается от снимка ДО, хотя изменённые поля билдер вообще не отправлял. Обычная
  сверка «отправленные ключи вернулись как отправили» такие изменения **не ловит by design**.
- Где: `_uac_campaign_patch_payload` / `_UAC_PATCH_FULL_KEYS` (38 ключей) — прод-путь
  `run_image_replace`; наблюдение — probe 3, `porg-pvrbl7mh`, МК `712714472`.
- Измеренные факты (сверка ВСЕХ 127 ключей detail; контроль — кампания-близнец `712714457`,
  того же типа/аккаунта, PATCH не получала):
  | # | Объект | Поле | ДО | ПОСЛЕ | Близнец `712714457` | Оценка |
  |---|---|---|---|---|---|---|
  | 1 | объявление `1915248839254163593` | `inheritableCallouts` | `{"policy":"CLEAR","assetValue":null}` | `{"policy":"INHERIT","assetValue":null}` | `1915248721141173292` до сих пор `CLEAR` | **дефект**, состав показываемых ассетов живого объявления изменён |
  | 2 | кампания `712714472` | `organic_search_enabled` | `null` | `true` | `null`, `updated_at` не сдвинут | **дефект**, вероятно необратимо |
  | 3 | кампания `712714472` | `device_types` | `[desktop,tablet,phone]` | другой порядок | — | безвредно (множество то же, порядок не значим) |
- Атрибуция однозначна: близнец PATCH не получал и остался в исходном состоянии по обоим полям.
- Почему №2 вероятно НЕОБРАТИМО: `organic_search_enabled` **нет** в `_UAC_PATCH_FULL_KEYS` — билдер
  его не шлёт, сервер материализовал значение сам как побочный эффект full PATCH. Тем же механизмом
  откат вернуть не может: `null` = «никогда не задавалось», записать `null` нечем.
  Контекст важности: проект сам считает `True` дефектом — `grid_content_verifier.py:120` поднимает
  `DYNAMIC_PLACES_ON`; по всем 70 `GdUnifiedCampaign` аккаунта Grid отдаёт `False`. Кампания стала
  выбросом на аккаунте.
- Вывод для кода: под REPLACE опасны не только ключи, которые мы отправили или забыли отправить, —
  сервер может **дописать/переключить поля, которых в payload нет вообще**, в т.ч. на СВЯЗАННЫХ
  объектах (объявление-зеркало). Наличие поля в whitelist ничего не гарантирует про поля вне него.
- ⚠️ **ПЕРЕЧЕНЬ ВЫШЕ НЕПОЛОН.** Он собран по **ОДНОМУ** живому прогону на одной кампании. Другие
  поля вне payload (и на кампании, и на связанных объектах) тоже могут меняться — отсутствие поля
  в таблице означает «не наблюдали», а не «не меняется».
- 🛑 **Это НЕ дефект к устранению и НЕ повод отказаться от транспорта.** У Мастеров кампаний **нет
  API**: замена картинки в МК возможна **только** через куки (web-api full PATCH), альтернативы не
  существует. Побочные эффекты — **известная и принятая цена единственного доступного транспорта**
  (решение Семёна: путь остаётся). Задача этой записи — чтобы будущий читатель их **ОЖИДАЛ и
  ПРОВЕРЯЛ**, а не искал способ обойти (его нет).
- ✅ Практическое следствие (что реально делать): перед заменой картинки в МК на **клиентском**
  аккаунте снимать **полный** detail кампании и связанных объявлений ДО и сверять **весь** его
  ПОСЛЕ тем же селектом (правило «Граница выборки» ниже) — не чтобы предотвратить (нечем), а чтобы
  ЗНАТЬ, что именно поехало, и при необходимости поправить руками.
- Статус: ❌ **побочные эффекты наблюдались живьём, НЕ откачены** (см. «Открытое состояние аккаунта»
  ниже). Прод-код не менялся и по этой записи меняться не должен: механизм на стороне сервера.
- НЕ помогло ранее: сверка «отправленные ключи вернулись» — по построению слепа к этому классу;
  сверка узким селектом (28 полей из 127) — дала ложное «побайтово совпало».

### ⚠️ ОТКРЫТОЕ СОСТОЯНИЕ АККАУНТА `porg-pvrbl7mh` — probe оставил 2 неоткаченных изменения (2026-07-19)
> **ТРЕБУЕТ ДЕЙСТВИЯ. Аккаунт РАБОЧИЙ, клиентский. Изменения внесены probe-прогоном 2026-07-19
> и НЕ откачены.** Решение о восстановлении — **за Семёном**, не за агентом.
1. **Восстановимо.** Объявление `1915248839254163593` (кампания `712714472`):
   `inheritableCallouts` `CLEAR` → `INHERIT`. Команда восстановления:
   `Grid UpdateAdaptiveTextAds, campaign 712714472, ad 1915248839254163593,
   inheritableCallouts: {"policy":"CLEAR"}`.
   ⚠️ Кампания **UAC-владеемая**, а прод-путь её намеренно пропускает
   (`content_images_routes.py:588`, `skip_cids=uac_owned`) → восстановление придётся гнать **вручную**,
   и это будет **ещё одна запись на рабочем аккаунте** (новый риск того же класса + баллы).
2. **Вероятно НЕвосстановимо.** Кампания `712714472`: `organic_search_enabled` `null` → `true`.
   Поля нет в отправляемых ключах, вернуть `null` нечем (см. запись выше). Практический эффект —
   кампания единственная на аккаунте с включёнными динамическими площадками
   (`DYNAMIC_PLACES_ON` по `grid_content_verifier.py:120`); при желании её можно перевести в `false`
   (как у остальных 70), но это НЕ возврат к исходному `null`.
3. Ранее зафиксированное и по-прежнему открытое: 2 осиротевшие картинки в библиотеке аккаунта
   (удаление = лишняя мутация, решение Семёна).

### 🔴 КЛАСС ОШИБОК: UAC_FULL_PATCH_REPLACE_DROPS_ASYMMETRIC_KEY (обобщение, 2026-07-19)
> **Читать ПЕРВЫМ при любой правке `_uac_campaign_patch_payload` / любого RMW-билдера (Grid, UAC).**
- **Признак класса (два условия вместе):** (1) full PATCH/update = **REPLACE**, ключ, которого нет
  в теле запроса, трактуется как пустой → **молча обнуляется**; (2) **read-форма богатая, write-форма
  урезанная и/или названа ИНАЧЕ** → наивное «скопировать ключ из detail» либо не срабатывает
  (`if k in detail` выбрасывает), либо пишет невалидное.
- **Уже потеряно ШЕСТЬ раз одной природы** — это не серия случайностей, а системная дыра:
  | # | Что потеряли | read → write |
  |---|---|---|
  | 1 | быстрые ссылки (sitelinks) | `assetValue` → `sitelinkSetId` |
  | 2 | уточнения (callouts) | набор → id-набор |
  | 3 | отображаемая ссылка | `linkTail` → `displayHref` |
  | 4 | текст кнопки | `button.customText` не читался вовсе |
  | 5 | автотаргетинг | `relevance_match_categories` (объекты с флагами) → `relevance_match` (плоские списки) |
  | 6 | условие ретаргетинга | `ca_retargeting_condition.goals[]` (объекты, `id` числом) → `{"id": "<строка>"}` |
- **Порядок действий, чтобы не искать СЕДЬМУЮ по факту потери:**
  1. НЕ сверять по счёту ключей — совпадение 33 vs 33 при разном составе уже обманывало (раунд 1).
     Сверять **множества имён** (MISSING/EXTRA) билдера против браузерного PATCH из HAR.
  2. Прогонять сверку на detail, где поле **НЕПУСТОЕ** — на пустом поле дыра невидима
     (`ca_retargeting_condition` не был виден по HAR-64, там значение было `null`).
  3. Для каждого ключа detail, которого нет в whitelist, спросить: не он ли пишется под ДРУГИМ
     именем? Известные асимметрии выше — проверять по таблице, а не по интуиции.
  4. Write-форму **не угадывать** по именам enum'ов/полей — нужен HAR с непустым значением
     (запрет действует с раунда 1, см. `relevance_match`: угаданная деривация записала бы неверный
     таргетинг, что ХУЖЕ пропуска).
  5. Ключ, которого браузер не шлёт при пустом значении, — **не слать пустой СТРУКТУРОЙ**
     (`{}`, `[]`, `{"condition_rules": []}`): под REPLACE это очистка настройки.
     Правило про структуры, **не про `null`**. Голый `null` очисткой не является — терять в нём
     нечего, поэтому наш билдер осознанно шлёт `reserve_landing_id: null` в 8 эталонах и
     `tracking_params: null` в `4har e847` там, где браузер ключ опускает. Это признано
     корректным (проверка 2026-07-19): под REPLACE «ключ = null» и «ключа нет» эквивалентны.
     Обратное — если ловите расхождение, сначала докажите, что сервер их различает.
  6. Точки прерывания между партиалом и full НЕТ (`_uac_patch_campaign_texts`) → «попробовать и
     посмотреть» на живой кампании НЕ является безопасной стратегией.
  7. 🔬 **Обязательный шаг перед словами «состав сошёлся»: сверять по ВСЕМУ HAR-корпусу, а не по
     1-3 эталонам.** Метод (воспроизводим, стандарт с 2026-07-19): собрать payload билдером на
     живом detail и сверить состав ключей и значения со **всеми** браузерными `PATCH …/uac/campaign/*`
     со статусом 200, какие есть в `_har/` + архиве HAR из `~/Downloads`. Именно переход
     с 3 эталонов на **16** вскрыл сразу ДВЕ неверные записи в этом журнале
     (`relevance_match.categories` «только порядок» и `field_to_use_as_*` «браузер не шлёт»).
     На малой выборке расхождение выглядит как совпадение — «сошлось на эталоне» без указания
     ЧИСЛА эталонов считать непроверенным утверждением.
  8. 📏 **ГРАНИЦА ВЫБОРКИ — ОБЩЕЕ ПРАВИЛО (введено 2026-07-19 после третьего провала подряд).**
     **Вывод «совпало / сошлось» действителен только в пределах той выборки, на которой он сделан.
     Сверяющая выборка обязана быть НЕ УЖЕ проверяемого утверждения — иначе получаешь ложное
     подтверждение, а не подтверждение.** Практически:
     - После записи (full PATCH / full-replace) сравнивать **ВЕСЬ** detail объекта и **ВСЕ**
       связанные объекты-зеркала, **ТЕМ ЖЕ селектом**, каким снят снимок ДО. Снимок ДО и снимок
       ПОСЛЕ разными селектами = сверка невалидна, сколько бы полей ни совпало.
     - В отчёте писать **знаменатель**: «28/127 полей», «3/16 эталонов», «35/60 кампаний».
       Формулировка «совпало побайтово» без знаменателя считается НЕПРОВЕРЕННЫМ утверждением.
     - Узкая выборка ловит только то, что в неё попало; всё вне неё она объявляет «без изменений»
       молча — это не молчание об отсутствии дефекта, это отсутствие проверки.
     Три реальных провала одной природы в одной задаче (все дали ложный вывод, все вскрыты
     расширением границы): **3 эталона вместо 16** (см. п.7) → «отличается только порядком»;
     **35 кампаний вместо 60** → ложное «расхождений нет»; **28 полей вместо 127** → ложное
     «снимки совпали побайтово», за которым скрылись два неоткаченных изменения на рабочем
     аккаунте (`UAC_FULL_PATCH_SIDE_EFFECTS_OUTSIDE_PAYLOAD`).
     ⚠️ Правило шире whitelist'а: сверять надо не «отправленные ключи», а **весь объект** —
     сервер меняет и то, чего в payload нет.
- НЕ помогло ранее: добавление имени write-ключа в whitelist без деривации (`content_ids` — ключа
  нет в detail, `if k in detail` делает это no-op); сверка по количеству ключей; вывод write-формы
  из урезанного HAR (`"<omitted…>"`); **сверка после записи узким селектом / по подмножеству полей
  (28 из 127) — даёт ложное «совпало побайтово», этим уже пропущены 2 живых дефекта.**

### GRID_RMW_DISPLAY_HREF_WIPED — отображаемая ссылка стиралась Grid full-replace (2026-07-17)
- Симптом: 229 из 244 объявлений копии с `DisplayUrlPath=None` (в источнике `лиды-для-бизнеса`).
- Где: cookie/Grid, `create_set_feeds._grid_update_adaptive_ads` (UpdateAdaptiveTextAds = полная замена),
  RMW-чтение `grid_finalize.GridClient.adaptive_ads_for_update`.
- Root-cause: RMW-чтение не запрашивало отображаемую ссылку → её не было в item → full-replace обнулял.
  Асимметрия имён полей: читается как `linkTail` (GdAdaptiveTextAd), пишется как `displayHref`
  (GdUpdateAdaptiveTextAdInput). Значение — ХВОСТ пути, НЕ полный URL (live 17.07.2026).
- Решение: `grid_finalize.py:2217` — `linkTail` в селекцию, `:2256` — в dict как `displayHref`;
  `create_set_feeds.py:757-772` — гард `it['display_href'] or cur['displayHref']` по образцу adPrice;
  `_apply_combo_button` (`:818`) — `displayHref` в список проносимых ключей (второй full-replace стирал бы).
- Статус: ✅ подтверждено live 17.07.2026 (porg-r7ro6tei, РСЯ 712850045, черновики): обе ветки —
  preserve из RMW и явный `display_href` — updated:1, перечитывание даёт `linkTail='лиды-для-бизнеса'`,
  5 картинок целы. Прогоном полного копирования НЕ проверено.
- НЕ помогло ранее: — (первая правка). Гипотеза «обрезка по байтам в direct_copy.py:1330» — ОПРОВЕРГНУТА
  (там len() кодпойнтов, гард не срабатывает); не повторять.

### COPY_TRAILING_PUNCT_STRIPPED — хвостовой «!» съедался у нетронутых строк (2026-07-17)
- Симптом: 75 объявлений копии: `...прямо сейчас!` → `...прямо сейчас`.
- Где: `copy_steps.py:step_adaptive_creatives` (1054/1059) → `text_norm._trim_clean` →
  `_strip_dangling_word_tail` (`text_norm.py:108`, безусловный `rstrip(" .,;:!?-")`).
- Root-cause: `_trim_clean` звался БЕЗУСЛОВНО, даже когда строка короче лимита → чистка «оборванного
  хвоста» применялась к строке, которую никто не обрезал.
- Решение: обрезать только при превышении лимита (`out if len(out)<=56 else _trim_clean(out,56)`).
  `text_norm.py` НЕ трогали — он общий с create-set, там срез у LLM-текстов осмыслен. Тот же инвариант,
  что уже принят для числового хвоста (ревью 06.07: чистить только то, что обрезали мы).
- Статус: 🟡 ждёт прогона копирования.
- НЕ помогло ранее: —

### COPY_ADAPTIVE_ONE_IMAGE_INSTEAD_OF_FIVE — у комбинированных объявлений копии 1 картинка вместо 5 (2026-07-17)
- Симптом: источник — 5 картинок на adaptive РСЯ-объявлении, копии — по 1.
- Где: `copy_steps.step_adaptive_creatives`; v5 переносит только одно поле `AdImageHash`, остальные 4
  живут лишь в Grid → доливки не было.
- Решение: при `image_mode=upload` доливаем хэши ЦЕЛЕВОГО аккаунта из `body["image_hashes"]`
  round-robin до 5 (`copy_steps.py:1069-1082`). Пул пуст → не трогаем.
- Статус: 🟡 ждёт прогона копирования.
- НЕ помогло ранее: —

### COPY_ORGANIC_PLACEMENT_NOT_APPLIED — isOrganicSearchEnabled/placementTypes не переносились при копировании (2026-07-17)
- Симптом: после копирования через v5-путь (`porg-mushirne` → `porg-jh2si7rh`) `step_settings_diff` показывал 4/5 кампаний с расхождением по `isOrganicSearchEnabled` (src=false, tgt=true) и `placementTypes` (src=[SEARCH_PAGE], tgt=[ADV_GALLERY,SEARCH_PAGE]).
- Где: `grid_finalize.py:set_campaign_organic_and_placement`, `copy_steps.py:step_fix_organic_placement`.
- Root-cause (двойной):
  1. `isOrganicSearchEnabled` = `biddingStategyWithPlatforms.platforms.organic`, `ADV_GALLERY в placementTypes` = `platforms.gallery`. Оба поля — производные стратегии. Функция `set_campaign_organic_and_placement` ставила только кампанейные флаги (`base["isOrganicSearchEnabled"]`, `base["placementTypes"]`), но НЕ патчила `platforms.organic/gallery` в стратегии — мутация эховала целевые значения (organic=True, gallery=True) → ничего не менялось. Grid возвращал `updated:[id]` без ошибок, что маскировало проблему.
  2. Для OPTIMIZE_CONVERSIONS + avgCpa=None (кампании типа `WB_MAXIMUM_CONVERSION_RATE`) — `_strategy_update_payload` неправильно выставлял `AUTOBUDGET_AVG_CPA` (требует avgCpa) вместо `AUTOBUDGET` → мутация падала с `CANNOT_BE_NULL:avgCpa` → кампания попадала в unsupported. Исправлено: avgCpa=0/None → `AUTOBUDGET` (round-trip «Максимум конверсий»).
- Решение (`grid_finalize.py`, 2026-07-17):
  1. В `set_campaign_organic_and_placement` (строки 2028-2033): после установки кампанейных флагов патчим `biddingStategyWithPlatforms.platforms.organic = is_organic` и `.gallery = "ADV_GALLERY" in pts_str`.
  2. В `_strategy_update_payload` (~480): `OPTIMIZE_CONVERSIONS + avgCpa=None` → `AUTOBUDGET` (не `AUTOBUDGET_AVG_CPA`).
  3. В `_unified_campaign_update_from_edit_row` (~678): `MULTIPLE_CPA` → `_unsupported_strategy` (write-enum 'MULTIPLE_CPA' недействителен в Grid).
- Результат: 712850009 (AUTOBUDGET) исправлена — organic False, pts [SEARCH_PAGE], стратегия AUTOBUDGET→AUTOBUDGET ✓. 712850007/712850008/712850299 — пропущены (DEFAULT/OPTIMIZE_CLICKS/MULTIPLE_CPA — нет безопасных write-enum).
- Статус: ✅ подтверждено живым зондом porg-jh2si7rh 712850009 (2026-07-17). Интегрировано в `step_fix_organic_placement` (copy_engine.py вызывает до step_settings_diff).
- НЕ помогло ранее: установка только `base["isOrganicSearchEnabled"]=False` и `base["placementTypes"]=["SEARCH_PAGE"]` без патча platforms — Grid принимал мутацию (updated:[id], 0 errors) но значения не менял.

### COPY_SETTINGS_DEFAULTS_ON_EXISTING_COPIES — 3 галочки Settings остаются на дефолтах Директа (2026-07-17)
- Симптом: у копий `hasAddMetrikaTagToUrl`/`hasExtendedGeoTargeting`/`isAlternativeTextsEnabled` = true, в источнике false (5/5 кампаний porg-mushirne → porg-jh2si7rh). Факт 2026-07-17: в porg-r7ro6tei 712850040 все три ДО СИХ ПОР true.
- Где: `copy_steps.py:step_settings_diff` (был report-only) + `_fix_v5_settings` (новый, `copy_steps.py:1310`).
- Root-cause: на создании закрыт белым списком `_COPY_SETTINGS_WHITELIST` (`direct_copy.py:90`), но у УЖЕ созданных копий (и при фолбэк-пересоздании кампании без Settings) чинить было нечем — постпроцесс копирования `set_campaign_invariants` не зовёт (и не может: падает на enum 'DEFAULT').
- Решение: автопочинка в `step_settings_diff` ДО сверки — v5 `campaigns.update` `TextCampaign.Settings` значением ИСТОЧНИКА 1:1, затем перечитывание цели. Только TEXT_CAMPAIGN; Grid не отдал поле → не трогаем.
- ⛔ Путь записи — v5, НЕ Grid: v5 update проходит на стратегии DEFAULT (ручные ставки), которую Grid-апдейт пропускает как `_unsupported_strategy`.
- Статус: ✅ подтверждено живым зондом porg-r7ro6tei 712850040 (2026-07-17): true/true/true → false/false/false одним update, повтор = no-op (идемпотентно), SMART_CAMPAIGN → skip; кабинет возвращён в исходное состояние.
- Проверено фактом (НЕ по созвучию имён): v5 `ADD_METRICA_TAG` YES→NO ⇒ Grid `hasAddMetrikaTagToUrl` true→false. Update с одной опцией — ЧАСТИЧНЫЙ: прочие 13 опций Settings не поехали (сверка всего массива до/после).
- НЕ помогло ранее: `set_campaign_invariants` (Grid) на копиях — падает на enum 'DEFAULT' (см. STATE_COPY_OTHER).

### V5_PROMOTIONS_INVALID_FIELDNAMES — promotions.get возвращал пустой список (2026-07-17)
- Симптом: `snapshot.promotions=0` на копировании — промоакции источника не попадали в снэпшот и не переносились на копию. `step_settings_diff` показывал `promoExtension DIFF: 4/5` даже на аккаунтах с живыми промо.
- Где: `work/slepki_direktologov/scripts/direct_copy.py:phase_pull` (функция pull промоакций через v5 API).
- Root-cause: `promotions.get` вызывался с `FieldNames=["Id","Type","Name",...,"Status","State"]`. v5 Яндекс.Директа возвращал `error 8000` на полях "Status" и "State" (не существуют в enum FieldNames promotions.get) → API возвращал пустой список → domain gate получал пустой input → `snapshot.promotions=0`.
- Решение (`direct_copy.py`, 2026-07-17): удалить "Status" и "State" из `FieldNames` в `phase_pull`. Оставить: `["Id","Type","Name","Description","Amount","AmountPrefix","AmountUnit","Promocode","Href","StartDate","EndDate"]`.
- Статус: 🟡 код задеплоен + `direct-copy.service` рестартован (2026-07-17). Верификация — при следующем полном запуске copy job (должно показать promotions > 0 в снэпшоте).
- НЕ помогло ранее: — (первое обнаружение).

### CT0000_GROUPS_FALLBACK_TO_SINGLE_TOVARNAYA — ЕПК/аудиторные группы слепка сворачивались в 1 «Товарная галерея» (2026-07-17)
- Симптом: tp5-кампании слепка `avto_sk` (и `avtolajt_bu`/`sk_krs`) в любом аккаунте получали ровно 1 группу «Товарная галерея» вместо правильной структуры архива. Также tp1 автотаргет-слепков «пропускалась» — возвращалась «пак недоступен (мост M3?)» даже при живом M3, потому что у них нет ключей в паке.
- Где: `create_set_tp1_builders.py:_struct_items` (~1624) + `_tp1_pack_groups` (~738) + фолбэк-блок (~821).
- Root-cause (тройной):
  1. `_struct_items` строка 1624: `if not ct or ct == "ct0000": continue` — пропускала ВСЕ позиции ct0000-слепков → `_items=[]`.
  2. При `_units=[]` и пустом M3-паке (нет ключей у автотаргет-дилера) → `groups=[]` → fallback ~864-877 создавал одну «Товарную галерею».
  3. Ранний выход `_tp1_pack_groups` (~738): `not pack and not (with_shopping)` → `{"skipped":"пак недоступен"}` — срабатывал ДО фолбэка даже когда M3 жив (пак=пустой, не сломан).
- Решение (`create_set_tp1_builders.py`, 2026-07-17, финальный md5 `0f16091d40a2850db07e5f2269522060`):
  1. **Ранний-выход bypass** (~738): перед `return {"skipped":...}` читает `_pack_read_glitch()`; если не сбой — проверяет структуру на ct0000+семантичный-gk. Для tp5-пути (`only_gks` задан) — bypass всегда. Для tp1-пути (`only_gks=None`) — bypass если в структуре слепка для tp_code есть хоть одна позиция с явным gk (!= aon_n000).
  2. **Фолбэк-блок** (~821): при `_units=[]` (снят гейт `_og is not None`) — читает ct0000+явный-gk позиции из `_load_struct`. Фильтр `aon_n000` отклоняет шумовые gk. `(_og is None or _igk in _og)` корректно фильтрует при обоих путях. Ставит `_multi=True`.
  3. **Skip-condition** (~848): `if not data.get("positive") and not (ct == "ct0000" and _gk): continue` — пропускает только реально пустые; ct0000+gk без ключей (норма для ЕПК) — допускает.
- Верификация:
  - avto_sk (porg-vfdnaolu): 10 ЕПК-кампаний, PASS — «Макс»→1гр «ЕПК макс», «Рет»→1гр «ЕПК рет», «3 гр»→3гр. v501 + Grid.
  - avtolajt_bu (porg-yzw6hkyk): tp1 1 кампания 3 группы (Купить б/у авто/Кредит/Рассрочка); tp5 4 кампании по фидам 3 группы каждая (Макс/Lul/Все); tp7 12 кампаний, ГОРОД→Краснодар. v501 + Grid подтверждено.
  - sk_krs (porg-usmc4253): tp1 ожидаем 2 группы (Краснодарский край, Товары — марка модель) + 8 tp7 — прогон 2026-07-17.
- Статус: ✅ подтверждено avto_sk + avtolajt_bu + sk_krs (2026-07-17).
- НЕ помогло ранее: только bypass для `only_gks` (первая попытка) — не помогал для tp1 (only_gks=None → `not None=True` → всё равно return "пак недоступен").

### HARDCODED_CITY_IN_SLEPOK_NAMES — город аккаунта-донора запечён в именах позиций слепка (2026-07-16)
- Симптом: слепок `avtolajt_bu` tp7 нёс позицию «ТК · Краснодар» → при создании РК на аккаунте ЛЮБОГО другого города имя кампании всё равно содержало «Краснодар» (город донора, с которого снят слепок).
- Где: tp7, `slepki_structure.json` (avtolajt_bu / «С пробегом» / tp7) + `create_set_plan.py:_emit_struct`. Имя tp6/7 строится `_build_name(cat=g["name"])`, где `g["name"]` = `f"{gname} - {label}"` из `_slepok_struct_groups` (`create_set_context.py:223-226`). `camp_names` на создание tp6/7 НЕ влияет (`structure_to_campaigns` — только tp1–tp5).
- Root-cause: структура слепка — шаблон, но имена позиций хранили конкретный город донора; параметризации города не было.
- Решение (2026-07-16, direct_fixer): (1) структура → токен `ГОРОД` (`group.name` «ТК · ГОРОД», `item.t` «ТК - Общая - Автотаргетинг - ГОРОД», `camp_names[0]` «ТК | ГОРОД [tk_kras_zqf]»; `item.camps` = исторический архив, НЕ тронут). (2) `create_set_plan.py`: константа `_CITY_PLACEHOLDER = "ГОРОД"` + подстановка города аккаунта в `name/group/label` в `_emit_struct` — СТРОГО ПОСЛЕ фильтра `sel_pos` (пользователь выбирает позицию по шаблонному имени). Гарды: пустой `city` → НЕ заменять (иначе «ТК · » без города) + warning в план; мультигород («Краснодар, Ростов») → `city.split(",")[0].strip()`. Токен `ГОРОД` уникален — в структуре 0 других вхождений → правка де-факто scoped на avtolajt_bu.
- Статус: 🟡 ждёт прогона (py_compile OK; офлайн-симуляция `_slepok_struct_groups`: city=Краснодар → имя байт-в-байт как до правки, другой город → его город, «ТК · Бренд»/«ТК · Регионы» не тронуты). Live-создание НЕ гонялось.
- НЕ помогло ранее: — (первая параметризация города в именах позиций).
- Известный эффект: UI «Структура слепков»/экспорт для tp6/7 берёт `item.t` как есть (`routes_slepki_edit.py:250`) → в превью видно шаблонное «ГОРОД» (в план/создание уходит реальный город). Подстановку в UI-превью решаем отдельно.

### TONE_OF_VOICE_MISSING_AGENTS — 4 слепка без agent-профиля → generic-контент (2026-07-16)
- Симптом: `get_agent('piterkina')`, `get_agent('avto_sk')`, `get_agent('avtolajt_bu')`, `get_agent('sk_krs')` возвращали None → генерация падала на generic-промпт без фирменного голоса → тон-судья давал <50.
- Где: `ai_agents.py` — AGENTS не содержал этих 4 ключей. CROSS_SIGNATURE также отсутствовал для 6 стартовых слепков (salamahin/gordeeva/zubakin/chepelev/tumashenko/kuderko).
- Root-cause: слепки добавлены в структуру (slepki_master) позже первичного разворота AGENTS.
- Решение (2026-07-16, direct_copywriter, task #43):
  1. Добавлены 4 AGENTS: piterkina/avtolajt_bu/avto_sk/sk_krs (name/tagline/site_fit/promo/system).
  2. Добавлены 4 AGENT_ADS: piterkina (10/5/4), avtolajt_bu (10/3/4), avto_sk (0/1/0 — фид), sk_krs (0/1/4 — фид).
  3. Добавлены 10 CROSS_SIGNATURE: 6 старых без сигнатуры + 4 новых. Итого 15 ключей.
  4. `build_titles_messages` + `build_campaign_messages`: добавлена инструкция «≥2 фирменных фразы».
- Чинит ли уже созданные РК: НЕТ. Только будущие прогоны.
- Статус: 🟡 задеплоено 2026-07-16 (md5 f33dc7efbfe1226ea6b84a28a6a81a76 Mac==LXC101). Верификация тон-судьёй (≥50/60) — при следующем прогоне этих слепков.

### OFFLINE_TONE_SCORING_PATH — добавлен оффлайн-путь тон-скоринга без живых кампаний (2026-07-16)
- Симптом: `check_tone_of_voice.py` умел оценивать голос только по живым кампаниям (читал Grid по campaign_ids). Нельзя было проверить сгенерированный контент до создания РК.
- Где: `tools/check_tone_of_voice.py:score_offline()` (новая функция), `tools/tone_baseline.py` (новый файл-харнесс).
- Решение (2026-07-16, direct_copywriter):
  1. `check_tone_of_voice.py` — добавлена `score_offline(slepok, site_type, titles, texts)`: вызывает `build_voice_reference()` + `judge_voice()` напрямую, без чтения кабинета. Возвращает тот же формат `{voice_score, verdict, ...}`.
  2. CLI extended: `--offline --agent X --site-type Y --content-file file.json` — работает без `job_id`.
  3. `tools/tone_baseline.py` — новый файл: SLEPOK_SITE_TYPES (20 пар), corpus-mode (AGENT_ADS as sample), generate-mode (OpenRouter gen), per-slepok baseline table. CLI `--mode corpus/generate --slepok X --site-type Y`.
- Чинит ли уже созданные РК: НЕТ. Инструмент превентивный — для проверки до создания.
- Статус: 🟡 задеплоено 2026-07-16 (md5 check_tone_of_voice `984007acd73b3a3c7b58aa05674ebfd8` Mac==LXC101; tone_baseline `cd0cb39daeaff093c4deea205f67d3d4`). Baseline scored: все 12 directologist-слепков ≥85/100 corpus-mode. Живой прогон РК — не запускался (creation паузировано).

### SK_KRS_AGENT_ADS_GENERIC_TEXTS — sk_krs 1 generic текст → score 20/100 (2026-07-16)
- Симптом: `tone_baseline corpus sk_krs/Мультибренд` → voice_score=20. Единственный текст «Продажа новых автомобилей в Краснодаре — купите автомобиль на выгодных условиях!» не содержал ни одной фирменной фразы.
- Где: `ai_agents.py` `AGENT_ADS["sk_krs"]["texts"]` — 1 текст, добавленный в task #43 как placeholder.
- Root-cause: при добавлении sk_krs (task #43) в AGENT_ADS texts вставлен generic без фирменных УТП; фид-слепок не генерирует тексты через LLM, тон-судья видит только корпус.
- Решение v1 (2026-07-16, direct_copywriter, task #43): промо-тексты из CROSS_SIGNATURE (Trade-In/Одобрение/СК Авто). score 20→100 corpus. DATA GAP — нет реального кабинета.
- Решение v2 (2026-07-16, direct_copywriter): заменены РЕАЛЬНЫМИ текстами из кабинета ТК №115016900
  (скрин ТК 6). Правки перед заливкой:
  1. `AGENT_ADS["sk_krs"]["titles"]`: [] → 5 реальных заголовков ТК. «в Краснодаре» → «в СК Авто» (3 из 5).
  2. `AGENT_ADS["sk_krs"]["texts"]`: «Гарантия качества» → «Заводское качество»; «Гарантия и выгодные цены» → «Выгодные условия и цены» (FORBIDDEN_CLAIM_RE). «Государственная программа» — FORBIDDEN_CONTENT_RE не триггерит, оставлена.
  3. DATA GAP закрыт: корпус = прямые объявления кабинета.
- Чинит ли уже созданные РК: НЕТ. Фид-тексты генерируются LLM и подтягивают corpus только через few-shot в промпте.
- Статус: ✅ задеплоено 2026-07-16 (md5 `664768cf6f7df01d0c38d9cc9da24f01` Mac==LXC101). score 100/100 corpus-mode (5 заголовков + 3 текста). FORBIDDEN: 0/8. Гео-нейтральность: OK.
- НЕ помогло ранее: — (v2 = финальный фикс).

### STOP_PHRASE_150PCT_TRADEIN_IN_TITLES — «трейд-ин До 150% цены авто» в заголовках Марки/Модели (2026-07-15)
- Симптом: фраза «трейд-ин До 150% цены авто» появилась в заголовках autotarget-кампаний Марки/Модели (porg-asfbs7qe, slepok kryuchkova, кампании 712819362/381/410/421). Inflated-claim на stop-листе, но просочился в live.
- Где: `create_set_assets.py:_upgrade_credit_titles` → шаблон поз.6 (brand_real=True): `f"{brand} трейд-ин. До 150% цены авто"`. Цикл НЕ вызывал `_bad_ad_title`. Существующий стоп-регэксп `text_norm.py:355` использовал `[^.]{0,24}` — точка между «трейд-ин» и «150%» рвала совпадение.
- Root-cause: двойная дыра: (а) hardcoded template содержал inflated-claim; (б) стоп-регэксп не ловил cross-sentence вариант с точкой перед «До 150%».
- Решение (2026-07-15, `direct_copywriter`):
  1. `create_set_assets.py:332` — шаблон `f"{brand} трейд-ин. До 150% цены авто"` → `f"{brand} трейд-ин. Оценка онлайн"`.
  2. `create_set_assets.py:308` — `"Трейд-ин до 150% цены авто. Оценка онлайн"` → `"Трейд-ин выше рынка. Оценка онлайн"`.
  3. `text_norm.py:_bad_ad_title` — добавлен паттерн `r"(?i)\bдо\s*(?:1[0-9]{2}|[2-9][0-9]{2})\s*%\s*(?:цены|стоимости)\s+авт"` (ловит cross-sentence и LLM-вывод).
  4. `text_norm.py:_bad_ad_text` — тот же паттерн для текстов.
  5. `create_set_assets.py:_upgrade_credit_titles` loop — добавлен `_text_norm_bad_title(line)` safety-net на каждый кандидат (шаблон и seq).
- Чинит ли уже созданные РК: НЕТ. Фикс только для будущих прогонов.
- Статус: ✅ задеплоено 2026-07-16 (Mutagen + `systemctl restart direct-create direct-create-worker`). Найден доп. bypass (2026-07-16): early-return в `_upgrade_credit_titles:296` возвращал `seq[:cap]` БЕЗ `_text_norm_bad_title` — если AI сгенерировал разнообразный набор (5+ credit-buckets, varied first words, all with digits), `_needs_credit_title_upgrade` → False → skip checks → bad title проходил. Фикс: early-return теперь `[t for t in seq if not _text_norm_bad_title(t)][:cap]`. Также исправлен GENERIC_TITLE_OVERRIDE ниже.
- НЕ помогло ранее: BRAND_ISOLATED_NOT_INTEGRATED (2026-07-10) изменил форму шаблона (убрал «{brand}.» изолят), но сохранил сам inflated-claim «150% цены авто».

### GENERIC_TITLE_OVERRIDE_BRAND_FIRST — `_needs_credit_title_upgrade` заменяет кастомный голос слепка шаблонами («Слепок Крючкова»/score 0/100) (2026-07-16)
- Симптом: live-заголовки кампаний kryuchkova Монобренд — «BAIC U5 Plus в Новосибирске. Кредит и первый взнос 0 ₽» (template f"{anchor}. Кредит и первый взнос 0 ₽") вместо kryuchkova-голоса («распродаём склад», «выгода до 45%», «2 платежа за наш счёт»). UTP-судья: score 0/100 (generic).
- Где: `create_set_assets.py:_needs_credit_title_upgrade:288` — `same_prefix = max(first_words.count(w) …)`. Для brand-first кампаний все 7 заголовков начинаются с марки («BAIC», «Chery» …) → same_prefix ≥ 5 → функция возвращает True → `_upgrade_credit_titles` ЗАМЕНЯЕТ весь AI-контент generic-шаблонами.
- Root-cause: ТРИ ветки `_needs_credit_title_upgrade` ложно срабатывали: (а) `same_prefix >= 4` — для brand-first ожидаемо; (б) `missing_numbers > 0` — kryuchkova органично пишет без цифр («Распродаём склад», «Срочно!»); (в) `len(buckets - {"other"}) < 5` — urgency/склад-фразы маппятся в "other" (это ГОЛОС, а не generic-признак).
- Решение v2 (2026-07-16, `direct_copywriter`): при обнаружении `_brand_fw` (бренд-слово 4+ позиций, не generic-авт/кредит-слово) — вся ветка `if _brand_fw` возвращает только `same_prefix_nb >= 4` (НЕ-бренд первые слова); numeric/bucket-проверки полностью пропускаются (они валидны только для не-brand-first контента). Не-brand-first путь = оригинальные три условия (regression guard). md5 = `e58c4470b51e402a5589cc6b5072e336`.
- Чинит ли уже созданные РК: НЕТ (только будущие прогоны).
- DATA GAP (не код, нужна slepki_master): `kryuchkova.ads.titles` пуст в AGENTS → few-shot = «(адаптируй свой тон)». Дозаполнить `AGENTS["kryuchkova"]["ads"]["titles"]` — зона `direct_slepki_master`.
- Статус: ✅ py_compile OK; repro v2 все OK: kryuchkova brand-first → `False`/голос сохранён; regression guard non-brand generic → `True`; stop-phrase → закрыт; legit до45%/до90% → passes. Задеплоено 2026-07-16 08:43 (+05). md5 Mac==LXC101.
- Остаточный edge + фикс v3 (2026-07-16, `direct_fixer`, задача #42): brand-first путь возвращал только `same_prefix_nb>=4` над НЕ-бренд первыми словами → если LLM выдавал N≥7 ИДЕНТИЧНЫХ brand-first заголовков («BAIC. Кредит. Условия»×7), `non_brand_fw` пуст → `same_prefix_nb=0` → вырожденный набор проскакивал как «голос» (не чинился). Фикс: dedup-guard ВНУТРИ brand-first ветки — `distinct_titles = len({t.strip().lower() for t in seq}); if distinct_titles < 3: return True`. Порог <3 безопасен: brand-first требует слово 4+ раз → seq ≥4 заголовков, разнообразный distinct-голос (≥3) остаётся False. bucket/numeric ветки НЕ возвращены (интенционально убраны). md5 Mac==LXC101 `ea3515a8ba0a117d98be27d03a206745`. Задеплоено (restart direct-create+worker, оба active).
- НЕ помогло ранее: — (первая правка остаточного edge; v2-решение выше корректно, но оставляло дыру для полностью идентичного brand-first вывода).
- Repro (задача #42, факт): CASE1 7×«BAIC. Кредит. Условия» → `True` (было `False`) ✓; CASE2 7 distinct BAIC-голос → `False`/сохранён ✓; CASE3 generic non-brand dup → `True` ✓; stop-phrase gate на distinct brand-first: needs=`False`, но early-return дропает «До 150% цены авто» (6 из 7 kept) ✓.

### UTP_AS_BRAND_BROKEN_TEXT_BODY — тавтологичный текст «Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽. КАСКО…» (2026-07-15)
- Симптом: в теле объявления появился сломанный текст: «Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽. КАСКО на 1 год…» — первое предложение неграмматично + тавтология.
- Где: `create_set_assets.py:_upgrade_credit_texts` (строки 363-401) + `_credit_title_anchor` (строка 190). Если первый текст в `seq` начинается с УТП-фразы (напр. «Первый взнос 0 ₽. …»), то `_credit_title_anchor` извлекает «Первый взнос 0 ₽» как `brand`. Тогда шаблон `f"Кредит на {brand}. Первый взнос 0 ₽. КАСКО…"` → «Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽. КАСКО…».
- Root-cause: `_credit_title_anchor` берёт первое предложение первого элемента seq и трактует его как «brand/anchor». Проверка `brand_low.startswith("авто")` не покрывает УТП-фразы с цифрами и словами «взнос»/«платёж».
- Решение (2026-07-15, `direct_copywriter`):
  1. `create_set_assets.py` — добавлен `_UTP_ANCHOR_RE = re.compile(r"(?i)\d|взнос|платёж|платеж|одобрени|выгода|каско|скидк")`. В `_upgrade_credit_texts`: если `brand` соответствует паттерну → `brand_is_utp=True` → force non-brand ветку шаблонов.
  2. Добавлен `_has_dup_clause(text)` — helper, проверяет повторяющиеся предложения (split по `. !/? + пробел`). В обоих циклах `_upgrade_credit_texts` добавлен `if _has_dup_clause(line): continue`.
  3. Текстовый шаблон поз.3 (brand) «Трейд-ин до 150% цены авто» → «Трейд-ин выше рынка» (заодно).
- Чинит ли уже созданные РК: НЕТ.
- Статус: ✅ задеплоено 2026-07-16 (Mutagen + restart). md5 Mac==LXC101.

### M3_SYNC_WIPES_TP67_KEYWORDS_DST_ONLY — собранные ключи tp6/7 зануляются ночным синком (2026-07-15)
- Симптом: собранные ключи tp7/tp6 пропадают из пака после крон-синка `sync_content_m3.py` (00:00/12:00). Пример: terehov `тк_lada_кс` 310 строк → 0, mtime файла = 04:00 (версия с M3). Массово: у 6+ слепков tp7-ключи обнулились. В UI «Структура слепков» кампании КС показываются как «автотаргетинг, ключей нет».
- Где: `home/seoadvanced/scripts/sync_content_m3.py` — `_build_dst()` (RAW→DST) + `_rsync_raw()` (pull M3→101). Пак: RAW=`/opt/neuro_content_raw`, DST=`/opt/neuro_content_local` (=NEURO_PACK_MOUNT, читается сервисом).
- Root-cause: харвест/добор-агенты писали ключи ТОЛЬКО в **DST**, а сборка DST идёт из **RAW** (`_build_dst` копирует RAW→DST по mtime; `_rsync_raw` тянет M3→RAW с --delete). RAW-канон пустой → build перезаписывает непустой DST пустым. C-страховка (`--filter=P *.txt`) защищает от УДАЛЕНИЯ при pull, но НЕ от ПЕРЕЗАПИСИ пустой версией.
- Решение (2026-07-15): (1) Процедурно — контент tp6/7 писать в **RAW+DST** (и push RAW→M3), не в DST-only. Восстановил из staging `_proposed_pack/` в RAW+DST+M3 (628 файлов; verify terehov `тк_lada_кс`=290 в RAW/DST/M3). (2) Код — **Safeguard D** в `_process_file`: `if ext=="txt" and src_size==0 and dst_size>0: skip` (не затирать непустой текст слепка пустым RAW). Push RAW→M3 даёт `utimensat Permission denied` на КАТАЛОГАХ (mtime папок M3) — не фатально, контент файлов едет (проверено wc -l на M3).
- Статус: ✅ подтверждено — RAW+DST+M3 непустые, py_compile OK, md5 Mac==LXC101. Safeguard D задеплоен.
- НЕ помогло ранее: C-страховка (защита *.txt от --delete) НЕ покрывает перезапись пустым — это другой класс.

### TP7_KEYWORDS_ZEROED_BY_COLLAPSE_OVERMERGE — схлопывание tp6/7 рвёт привязку ключей (2026-07-15)
- Симптом: после collapse tp6/7 КС-кампании показываются как автотаргет (ключей нет), хотя ключи в паке есть.
- Где: `scratchpad/slepki_harvest_2026-07-14/_collapse_recompute.py` — `apply_rule_A_tp67` / `_tp67_targeting_fingerprint`.
- Root-cause: RuleA схлопывает tp6/7 по fingerprint (camp_names, autotarget_cats, audiences, targeting_mode) — БЕЗ сверки ключевых слов. Две КС-кампании с РАЗНЫМИ ключами (Lada Granta vs Lada Vesta) дают одинаковый fingerprint → сливаются в синтетич бренд-gk (`mk___lada`), который НЕ совпадает с пак-файлами моделей → ключи повисают. terehov tp6 478→233, 262 синтетич gk.
- Решение (2026-07-15): откатил tp6/7 к pre-collapse из `.bak.safecollapse_*`. RuleA нельзя применять к КС-кампаниям без включения keyword-fp в fingerprint. Схлопывать безопасно только чистый автотаргет (нет ключей/аудиторий).
- Статус: ✅ откачено, привязка ключей восстановлена (gk = tp6/7 оригинальные, совпадают с пак-файлами).

### TP7_CATALOG_MARK_FILTER_ZEROED_NONBRAND — небрендовая товарка получала mark-фильтр → 0 страниц (2026-07-14)
- Симптом: tp7-товарка «Общие запросы» (ct0014, без марки) на каталог-фиде получала `listings_feed_filters` = collectionId CONTAINS[mark_*] → 0 страниц каталога.
- Где: `create_set_master_product.py` ветка `elif is_product and it_feed:` (~646). Небрендовая группа (нет c_brand) не попадала в модельную ветку (первый `if` требует `c_brand`), шла в elif → `else` → `_tp7_listings_minus_filters` (allow-list mark_*), а её страницы в mark_*-коллекции не входят → пусто.
- Решение (2026-07-14, direct_fixer): гейт `if not c_ct or c_ct=="ct0000"` расширен на `if not c_brand or not c_ct or c_ct=="ct0000": it_lff=[]`. Небрендовым группам mark-фильтр не ставим (весь каталог). Брендовые (есть c_brand, напр. ct0111) по-прежнему идут в else → mark-фильтр.
- Статус: 🟡 ждёт прогона. py_compile OK.
- НЕ помогло ранее: — (первый фикс).

### SINGLE_FEED_FALLBACK_KEY_DESYNC — фолбэк каталог-фида не открывался (feed_confirmed vs single_feed_fallback) (2026-07-14)
- Симптом: tp5/tp3/tp7 не создавались при single_feed на аккаунте без /yandex.xml, хотя пользователь подтвердил «Продолжить с другим фидом». Фолбэк на `yandex-catalog-model-design-custom-name.xml` не открывался.
- Где: гейты фолбэка читали `body.single_feed_fallback`, а UI (routes_jobs.py:144/186, test-драйверы) шлёт `feed_confirmed` — ключи не связаны. `create_set_plan.py:~423/432`, `create_set_feed_builders.py::_resolve_single_feed_variants:~875`.
- Решение (2026-07-14, direct_fixer): оба ридера принимают ОБА ключа. plan — `_sf_fb_confirmed = bool(body.get("single_feed_fallback") or body.get("feed_confirmed"))` в открытии фолбэка и warning. feed_builders — `_fb_body.get("single_feed_fallback") or _fb_body.get("feed_confirmed")`. Логика feed_confirmed (фолбэк только когда основной /yandex.xml реально отсутствует) не тронута.
- Статус: 🟡 ждёт прогона. py_compile OK.
- НЕ помогло ранее: — (первый фикс).

### STRUCT_NAME_TRANSLIT_ABTO — ломаная кириллица имени группы 1в1 («Abto Py») (2026-07-14)
- Симптом: tp1/tp2-группы в режиме 1в1 (_multi) получали имя-транслит «Abto Py» (= «Авто Ру», auto.ru) прямо из структурного `t` слепка.
- Где: `create_set_text_builders.py::_struct_items` и дубликат `create_set_tp1_builders.py::_struct_items` — имя = `it.get("t")` как есть; в _multi-режиме name = `_uname` (структурный) без нормализации.
- Решение (2026-07-14, direct_fixer, часть (а)): добавлен `_norm_struct_name` (карта `abto py→Авто Ру`, `abto→Авто` + пословный `\bAbto\b→Авто`), применён при чтении `t` в обоих `_struct_items`. Брендовые имена (BAIC X35) не трогаются. tp1 кодер-имена (не-multi путь) не затронуты.
- Статус: 🟡 ждёт прогона (часть (а) — кириллица). Часть (б) «похожий соседний кодер, иначе ct0001» — НЕ сделана: в _struct_items item без валидного ct (ct0000/пустой) уже отбрасывается (:294/:1495), группы до создания доходят только с валидным ct → «нет кодера» на уровне ДАННЫХ нет; переприсвоение валидного ct «похожему соседу» — без чёткого определения «похожести», риск misroute images/segment/adPrice + tp1. Требует решения Семёна.
- НЕ помогло ранее: — (первый фикс).

### MULTI_GROUP_NO_CODER_NAME — в _multi-ветке имя группы без кодер-префикса (2026-07-21, ИСПРАВЛЕНО)
- Симптом: у tp2/tp4 слепков с ct-коллизиями (>1 группы на один ct) 518/522 групп (porg-xjxpfxby) не имели ct-токена в имени. Кодер-часть (`ct0006_aon_n000_r0300_ct001_ag011_g00`) отсутствовала — имя состояло только из структурного `t` («Без Первого», «Автосалон Купить» и т.п.).
- Где: `create_set_text_builders.py:493-494` — в ветке `_multi=True` имя группы бралось напрямую из `_uname` (структурное название item), минуя `_text_group_name(ct, r_code, model)`.
- Решение (2026-07-21, direct_fixer): строка `"name": (_uname if (_multi and _uname) else ...)` заменена на `(display if _struct_names else _text_group_name(ct, r_code, _uname if (_multi and _uname) else display))`. Теперь дисплейное имя группы (_uname) становится «темой» кодер-имени, а не заменяет его целиком. dmp-путь (_struct_names) не изменён.
- Статус: 🟡 ждёт прогона (py_compile OK, логика проверена офлайн).
- НЕ помогло ранее: — (первый фикс).

### TP1_RSYA_VIDEO_MISSING_NOT_EMITTED_STRUCT_NAME — 0 видео: аудит не эмитил VIDEO_MISSING из-за структурных имён (2026-07-14, ДИАГНОЗ, НЕ пропатчено)
- Симптом: tp1 РСЯ 0 видео, хотя у моделей BAIC/Belgee/Haval/Москвич видео в пуле есть; шаринг модель→марка (`videos_for_ct` brand-fallback) реализован.
- Где: `campaign_spec_audit.py:881` `agid_to_ct[gid]=_ct_of_name(g.get("adgroup_name"))`; `_ct_of_name` (:135) ищет `ctNNNN` в имени. В режиме 1в1 (`_multi=True`, `create_set_tp1_builders.py:713`) имя группы = `_uname` (структурное, БЕЗ ct-токена) → `_ct_of_name`→ct0000 → цикл аудита `:902 if not ct or ct=='ct0000': continue` пропускает все объявления → `video_missing`=[] → VIDEO_MISSING не эмитится → deferred-video не докручивается. `videos_for_ct` brand-fallback НЕ достигается (аудит выходит на ct0000-skip). Delayed content_repair планируется штатно (queue_server.py:1754, repair_auto.py:517 без гейта на videos_deferred) — оркестрация не виновата.
- Решение: ЧАСТИЧНО (2026-07-21): фикс `MULTI_GROUP_NO_CODER_NAME` выше добавил кодер в имена tp2/tp4 групп (_multi) → _ct_of_name теперь будет находить ct у этих групп. Для tp1 остаётся актуальным (tp1_builders отдельный путь). Варианты (решение Семёна): (A) в _multi оставить структурное имя только tp2/tp4, а tp1/tp5 держать кодер-имя `_tp1_group_name` (ct в имени → аудит работает; но противоречит «имя как в кабинете» для tp1); (B) прокинуть в аудит slepok/site_type и строить name/gk→ct-карту из `_struct_items` (сложнее, живые имена усечены до 255/нормализованы кабинетом). Оба — дизайн-решение.
- Статус: 🟡 tp1 часть — ДИАГНОЗ (код-доказан), ждёт выбора варианта Семёном.
- НЕ помогло ранее: — .

### CONTENT_DB_SLEPOK_FALLBACK_OK — «generic вместо голоса слепка»: премиса не подтвердилась кодом (2026-07-14, разбор)
- Симптом (заявлен): при пустом ответе OpenRouter движок наполнил заголовки generic-шаблонами вместо БД-слепка Павлова.
- Разбор (direct_fixer, против актуального кода): (1) Ретрай на пустой content OpenRouter УЖЕ есть — `llm_providers._or_complete_url:489-494` (`OpenRouter: пустой content` → sleep(backoff)+retry, tries=2). Добавлять нечего. (2) Fallback `create_content.py:997` (`fallback = not good_t and not good_x` → БД-слепок `_slepok_content_get`) корректен: `good_t/good_x` наполняются ТОЛЬКО LLM-принятым контентом (raw_titles/raw_texts из M3/OR через инжектированные `_m3_complete_*` + гейты + 72b + repair). Локальные generic-шаблоны сюда НЕ попадают — они наполняют `content` в `_final_fill_campaign_content` (:967) ПОСЛЕ, и при `fallback=True` этот `content` перезаписывается БД-слепком (:999-1013). ⇒ при реально пустом LLM фолбэк на БД-слепок срабатывает. Премиса «generic заполнили good_t/good_x» кодом НЕ подтверждается.
- Остаточный реальный зазор (не заявленный): fallback требует good_t И good_x пустыми (AND). При частичном LLM (есть заголовки, нет текстов) БД-слепок не тянется, тексты generic-филлятся. «Починка» — редизайн сборки/ пер-секционный фолбэк ИЛИ fuzzy-детект generic (риск затереть валидный LLM) — дизайн-решение Семёна.
- Статус: НЕ пропатчено (премиса не доказана против кода + риск). Часть 1 (ретрай) уже в коде.
- НЕ помогло ранее: — .

### SITELINK_SET_NULL_SILENT — набор быстрых ссылок null БЕЗ ошибки в отчёте (2026-07-14)
- Симптом: `sitelink_set_id`=null, tp1 РСЯ без быстрых ссылок, в отчёте — ни ошибки, ни причины.
- Где: оба пути глотали исключения. `automation_runtime.py:_resolve_campaign_assets` (~2384: primary Grid `add_sitelink_set` в `except: → _get_or_reuse`, а пустой ответ БЕЗ исключения молча ронял в None без фолбэка) и `_get_or_reuse_sitelink_set` (Grid `except: pass`, v5 non-152 → return None молча).
- Решение (2026-07-14, direct_fixer, диагностика-первый-шаг): `_get_or_reuse_sitelink_set` получил опц. `warns` — реальные причины v5 (code)/Grid (исключение/пусто) пишутся в `warns` и в журнал (`print [sitelink-set]`). `_resolve_campaign_assets` наполняет `out["asset_warns"]`, фолбэк теперь и при пустом primary (не только исключение), финальный summary-warn при null. Caller `create_set_tp1_builders.py:803` мёржит `_assets["asset_warns"]` в `rep["warnings"]` (канал уже был). Полная починка создания набора — не делалась (причина станет видна из asset_warns/журнала живого прогона).
- Статус: 🟡 ждёт прогона (диагностика — причина null станет видна). py_compile OK.
- НЕ помогло ранее: — .

### TP1_RSY_3WAY_TARGETING_SPLIT — разведение tp1 РСЯ на Автотаргет / КС / КС+Автотаргет (2026-07-14, фича)
- Симптом (не ошибка, а фича): tp1 РСЯ мог быть либо чистым автотаргетом (реальные ключи ВЫБРАСЫВАЛИСЬ, спецключ `---autotargeting`), либо чистым КС. Комбинированного режима (спецключ автотаргета + реальные ключи одной кампанией) не было.
- Где: token/API-путь tp1. `create_set_plan.py::_set_plan_response` (развилка режимов tp1) → `create_set_tp1.py::run_create_set_tp1` → `create_set_tp1_builders.py::_create_tp1_campaign → _create_tp1_single → _build_tp1_from_pack → _build_tp1_adgroups` (Фаза 2 keywords.add).
- Решение (2026-07-14, direct_fixer): (1) новый читатель `_tp_seg_modes(slepok,site_type,tp,seg)` — берёт `tp.seg_modes[seg]` из slepki_structure.json (приоритетный источник, аналогично `_tp_seg_name_override`); нет → fallback на боевой профиль `_slepok_tp_modes` → дефолт `["КС"]`. (2) Развилка плана на 3 ветки по mode: `КС`→(at=F,keep=T,«КС»), `Автотаргет`→(at=T,keep=F,«Автотаргетинг»), `КС+Автотаргет`→(at=T,keep=T,«КС + Автотаргетинг»); в plan-item добавлен флаг `autotarget_keep_keywords`. (3) `keep_keywords` проброшен по всей цепочке билдеров; в `_build_tp1_adgroups` бинарь заменён на: `if autotarget and tp_code!="tp5"` → `_AUTOTARGET_KW`; `if (not autotarget) or keep_keywords` → реальные `_kw_clean(...)`. Комбинир. режим = И спецключ, И реальные ключи; имя группы `aon` (autotarget=True) — корректно.
- Обратная совместимость (доказано фактом): в текущей ПРОД-структуре **0 tp-узлов с `seg_modes`** → `_tp_seg_modes`=None везде → fallback байт-идентичен прежнему поведению. Старые режимы `КС`/`Автотаргет` дают ровно те же (at, ключи), что до правки (даже без флага keep — дефолт False). 3-я ветка активируется ТОЛЬКО если `КС+Автотаргет` явно задан в `seg_modes[seg]` (структуру ведут другие агенты — не тронута).
- Транспорт (проверено по ERRORS_JOURNAL): tp1 API-путь создаёт группы через **v501 adgroups.add** и льёт ключи **v5 keywords.add** — ЕДИНЫЙ транспорт, НЕ смешанный Grid+v5 (тот даёт фантом-ключи, см. `DMP_TP2_MIXED_TRANSPORT`). Комбинированный режим не вносит mixed-transport риска. Cookie-путь (`_create_tp1_via_cookie`) НЕ трогался (его группы и так несут ключи + `autotargeting=` отдельным флагом в `create_full`) — combined на cookie НЕ верифицирован.
- ⚠ Риск: на tp1 РСЯ `---autotargeting` даёт ШИРОКИЙ автотаргет (без категорийного сужения, в отличие от tp5 через Grid-профиль) — для tp1 РСЯ пока приемлемо (отмечено в коде `:296-303`).
- Статус: 🟡 ждёт живого прогона (нужен слепок с `seg_modes[...]=["...","КС+Автотаргет"]` от структурного агента). py_compile 3 файлов OK; offline mapping mode→(at,keep,keys) подтверждён.
- НЕ помогло ранее: — (первая реализация фичи).

### PER_ADGROUP_GROUPS_1V1 — движок схлопывал группы по ct (dedup), нужна раскладка 1в1 (2026-07-14)
- Симптом (не «ошибка», а фича): при ct-коллизии в структуре (напр. terehov tp2 ct0044 = «Поиск Chery марка» + «Chery марка-квиз») движок дедупил по ct → 1 группа на ct вместо N реальных групп кабинета.
- Где: `create_set_text_builders.py::_build_text_from_pack` (dedup `for ct in cts`), `create_set_tp1_builders.py::_build_tp1_from_pack` (`for ct in sorted(pack.keys())`), пак ключён по ct (`kontent_pack.py::read_keywords/gather` без group), `slepki_editor.py` (правка пака без group).
- Решение (2026-07-14, direct_fixer; A—E дизайна): (A) чтение опц. поля `gk` у item структуры. (B) `kontent_pack.py`: `_group_slug(gc/gk→слаг: убрать `ctNNNN_`+`_rNNNN`, filename-safe)`, `_read_lines_opt`(флаг найден/нет для фолбэка), `read_keywords/read_callouts(...,group="")` (per-group `{slepok}__{slug}.txt`/`_minus`+shared с фолбэком на легаси), 2-й проход `gather` → `out[ct]["_groups"][gk]` (top-level НЕ меняется; синтез top-level для ct без легаси-файла), `_normalize_gather` гео-чистит и positive групп. (C) `_struct_items` (без дедупа, с gk) + гейт в `_build_text_from_pack`: multi ТОЛЬКО при реальной ct-коллизии в структуре И не only_cts/dmp → строим по items (`pack[ct]["_groups"][gk]` фолбэк `pack[ct]`, имя из item `t`); иначе легаси per-ct. (D) тот же гейт в `_build_tp1_from_pack`. (E) `slepki_editor._pack_rel/_pack_rel_callouts/apply_edit_keywords/read_group_keywords` + `group=""` (shared-минус остаётся пер-ct общим).
- Обратная совместимость (offline доказано): `_group_slug` правило+идемпотентность на gk; `read_keywords(...,group='ct0044_aon…')` БЕЗ per-group файла == `group=''` (SAME); gather top-level legacy нетронут, `_groups` аддитивен; dmp(splits)→`_struct_items`=[] → легаси; py_compile+pyflakes чисто (новые символы без варнингов).
- Статус: 🟡 ждёт живого прогона. **ВНИМАНИЕ Семёну/verifier:** гейт СРАЗУ активируется для 41 tp-блока с существующими ct-коллизиями (terehov/kuderko/gordeeva/zubakin/…). Per-group пак-файлов ЕЩЁ НЕТ → все группы коллизирующего ct получат ОДИНАКОВЫЕ ключи (фолбэк `pack[ct]`), но РАЗНЫЕ имена (из item `t`); две группы с одним gc дают одинаковый gk → один per-group файл (для различения нужен авторитетный `gk` в структуре + харвест Кудерко per-group). Число групп на таких слепках вырастет. НЕ деплоено.
- НЕ помогло ранее: — (первая реализация фичи 1в1).
- **2026-07-14 (PROD-apply attempt, direct_neyrodirektolog — СТОП на верификации):** пак 1в1 залит (26500 файлов в M3+DST, аддитивно, `<slug>__<gk>.txt`, md5 M3==DST ✅, легаси `{slug}.txt`/`_minus_shared`/dmp/gen_ses не тронуты). Структура 12 слепков собрана per-group (16481 items, 0 битых gc). **Два блокера, оба доказаны фактом:**
  1. **Движок читает `it.get("gk")` (`create_set_text_builders.py:296` `_struct_items`), а НЕ `grp["name"]`.** При item-схеме `{c,t,gc}` без `gk` фолбэк `_group_slug(gc)` даёт ОДИН слаг на все группы одного gc (в ct все gc одинаковы) → **0/184 попаданий** в `pack[ct]["_groups"]`, все группы читают ct-агрегат (per-group-разбивка мертва). С `gk` (сырой) в item → **184/184** (live на kuderko/С пробегом/tp4). ⇒ item-схема ОБЯЗАНА быть `{c,t,gc,gk}` (не `{c,t,gc}`).
  2. **`_group_slug` (kontent_pack.py:1010) `re.sub(r"[^a-z0-9_]","_")` УБИВАЕТ кириллицу**, а gk/пак-файлы — кириллические (`_build_pack._slug` = `[^a-z0-9а-яё_]`). ⇒ `read_keywords(group=<кир.gk>)` не находит `{slug}__<gk>.txt`, отдаёт агрегат (баг: смок показал per_group==aggregate==3762). Ломает UI-просмотрщик ключей per-group. **Движок create НЕ затронут** — он берёт `gather()._groups` по СЫРОМУ gk из имени файла (`kontent_pack.py:1533` `gk=name[len(pref):]`), а не через `_group_slug`.
- Требуемый фикс (не сделан, ждёт Семёна): (a) в Stage-2 структуре item = `{c,t,gc,gk}` (gk сырой кириллический = имя файла/группы); (b) `_group_slug` regex → `[^a-z0-9а-яё_]` (одна строка, ASCII-входы типа gc не меняются, симметрично `_build_pack._slug`). Структура откачена в known-good (md5 c8ec8d2), пак оставлен на M3/DST (аддитивен, инертен для старой структуры без gk: `_multi`=False → `_groups` игнорируется). НЕ задеплоено, НЕ коммичено.
- **✅ 2026-07-14 (ФИНАЛ-apply, direct_neyrodirektolog — ЗЕЛЁНО, оба блокера подтверждены исправленными):** оба фикса уже были в коде (item-схема `{c,t,gc,gk}`, `_group_slug` kontent_pack.py:1011 держит кириллицу). Применено полностью: **Stage-1 пак** — дельта tp6/7+MANUAL дозалита аддитивно (rsync без --delete, per-slug) → M3=DST=**26861** `__`-файлов, md5 M3==DST на всей новой tp6/7-подветке (35/35) ✅, легаси/`_minus_shared`/dmp/gen_ses не тронуты. **Stage-2 структура** — site_types 12 слепков заменены per-group деревом (16748 items, схема ровно `{c,t,gc,gk}` 0-bad, tp1-tp7 code+title, one-container-per-item), dmp/gen_ses hash БАЙТ-идентичны до/после, md5 Mac==101 `eafe47b8`. **Stage-3** рестарт direct-create+worker active, 0 tracebacks. **Stage-4 смок ЗЕЛЁНЫЙ:** `?tab=slepki`=200, `?tab=create`=200; kuderko/С пробегом/tp4=**184 группы**; tp6+tp7 у kuderko/terehov, tp7 у scherbakova (tp6=0 — так в слепке, не баг). **PER-GROUP ДОКАЗАНО ФАКТОМ:** `read_keywords('С пробегом','tp4','ct0006','kuderko',group='рассрочка_без_процентов')`=**17** позитивов (рассрочка-специфичные), агрегат `group=''`=**3760** → distinct. Движок create читает новую структуру: `_struct_items` kuderko/С пробегом/tp4 = 171 buildable items (184 − 13 ct0000/by-name, скипаются by design), ВСЕ с gk. Структура ОСТАЁТСЯ применённой. НЕ коммичено (по указанию).

### GEO_STRIP_ORPHAN_PREPOSITION — после удаления города остаётся висячий предлог «в/на» (2026-07-14)
- Симптом: ключи после гео-чистки заканчиваются/висят на предлоге: «Haval купить в» (было «…в Самаре»), «Haval официальный дилер в». Город убран, предлог-связка «в» остался.
- Где: `geo_strip.py::strip_geo_tokens` — применяется при чтении пака (`kontent_pack.read_keywords/gather`), делает ключи геонейтральными.
- Root-cause: старая версия удаляла гео-токен (город/двусловный топоним), но НЕ трогала предшествующий предлог-связку («в/во/на/по/г/город/…»). Самотест даже закреплял `("авто в нижнем новгороде", "авто в")` как «норму» — неверно.
- Решение (2026-07-14, direct_fixer, только `geo_strip.py`): переписан `strip_geo_tokens` на токенный проход. (1) Помечает гео-токены: двусловные `_TWO_TOKEN_GEO` (пара соседних токенов, раньше однословных) + однословные `_SINGLE_TOKEN_GEO` (=`_GEO` ∪ дефисные из `_MULTI_GEO`). (2) Справа-налево снимает предлог-связку `_GEO_PREP` (в,во,на,по,г,гор,город,городе,города,обл,область,области,регион,регионе), если её сосед-справа — удалённый гео (снимается и цепочка «в городе <город>»). (3) Отбрасывает висячий хвостовой предлог. Операторные токены (`-самара`,`!в`,`+в`,`[`) НЕ трогаются на всех шагах. «в кредит»/«в наличии» СОХРАНЯЮТСЯ (после предлога НЕ-гео слово → предлог остаётся). Удалён мёртвый `import re` + `_MULTI_RE` (регекс-путь заменён токенным).
- Верификация: `py_compile` OK, `pyflakes` clean, `python3 geo_strip.py` self-test 17/17 «All OK» (в т.ч. 6 обязательных кейсов Семёна). Обновлён self-test: бывший `("авто в нижнем новгороде","авто в")` → `"авто"`; добавлены «в кредит»/«в наличии»/«в городе <город>»/haval-кейсы.
- Статус: 🟡 ждёт живого прогона (проверять: после гео-чистки в паке нет ключей, оканчивающихся на голый предлог; «в кредит»/«в наличии» целы). Локально self-test доказан, но на реальном dst/M3-паке НЕ прогонялось.
- НЕ помогло ранее: — (первая правка; прежняя версия сознательно оставляла предлог как «норму» — это и был баг).

### GEO_STRIP_MINUS_PARTS_G8 — гео-город убрать И из минус-частей фразы (2026-07-14, расширение)
- Решение Семёна (правило G8, `SLEPKI_REBUILD_PLAN.md:112,130`): гео-города удаляем не только из позитива, но и из-под оператора (`-`/`!`/`+`/`[`/`"`). Прежняя версия НАМЕРЕННО берегла операторные токены как минус-модификаторы — теперь это отменено для ПОЛНЫХ топонимов.
- Где: `geo_strip.py::strip_geo_tokens` (тот же читатель пака, что и ORPHAN_PREPOSITION выше).
- Решение (2026-07-14, direct_fixer, только `geo_strip.py`): добавлен хелпер `_geo_core(low_tok)` — снимает операторную обёртку (ведущие `!+-["`, хвостовые `]"`) и возвращает ядро-топоним; позитивный токен возвращается как есть; токен из одних операторов → `''`. В `strip_geo_tokens` шаг 1 теперь сверяет по `core=[_geo_core(t) for t in low]` (а не по `low` с пропуском операторных): и однословные `_SINGLE_TOKEN_GEO`, и двусловные `_TWO_TOKEN_GEO` (пара соседних ядер) матчатся И под оператором → выкидывается ВЕСЬ токен (оператор+город). НЕ-гео минусы (`-бесплатно`,`-фото`) и минус-обрезки, не совпавшие с полной формой словаря (`-самар` ≠ `самара/самаре/…`), остаются. Шаги 2-3 (орфанный/висячий предлог) не тронуты.
- Верификация: `py_compile` OK, `pyflakes` clean, `python3 geo_strip.py` self-test 20/20 «All OK» (добавлены обязательные кейсы: `-екатеринбург -кемерово`→убраны, `-краснодарский край`→убран, `-plus -тольятти`→`-plus` цел/тольятти убран, `-бесплатно -фото` целы, `-самар` обрезок цел; обновлены прежние `-тольятти`-кейсы, которые раньше ждали сохранения оператора). Только `geo_strip.py`, НЕ деплой.
- Статус: 🟡 ждёт живого прогона на реальном dst/M3-паке (self-test доказан локально).
- НЕ помогло ранее: — (первое расширение на минус-части; это сознательная отмена прежнего «беречь операторы», а не провалившийся подход).

### DMP_PROMO_PCT_GATE_FALSE_REJECT — гейт `_promo_usable_for_content` ложно режет PCT-промо dmp (в B2B-контенте нет скидочного %) (2026-07-14)
- Симптом: промо для dmp сгенерировано (7 вариантов), но к набору кампаний НЕ привязывается. Пилот: `result.promo = "…процент промо не совпадает с процентом в контенте"`.
- Где: `automation_runtime.py::_promo_usable_for_content` (~2685-2691). Файл `text_gen.py::_discount_pcts` (:968, регэксп `_PCT_DISC_RE` под скидочный %).
- Root-cause: `promo_pcts = {100}` (PCT-промо dmp), но `content_pcts = ∅` — B2B-формулировка dmp «до 150% лидов» НЕ парсится `_discount_pcts` как скидка. Старое условие `if promo_pcts or content_pcts: if promo_pcts != content_pcts: return False` → `{100} != ∅` → ложный reject любого PCT-промо, когда в контенте нет скидочного %.
- Решение (2026-07-14, scoped, только `automation_runtime.py:~2687`): конфликт по % только когда % указан в ОБОИХ множествах: `if promo_pcts and content_pcts and promo_pcts != content_pcts: return False`. Если `content_pcts=∅` — не отбраковывать промо по проценту. Backup `.editbak.20260714_085439`, py_compile OK.
- Безопасность для авто-слепков со скидками: при непустых обоих множествах поведение ПРЕЖНЕЕ (расходятся → reject). Меняется только случай, когда одно из множеств пусто (нечему конфликтовать). Прочие гейты (техчисло, кешбэк) не тронуты.
- Вторично (тот же коммит): в `_seed_one_slepok_promo` (~3633/3660) авто-фолбэк `neutral_new` («на новые авто»/«в кредит»/«по госпрограмме») просачивался в dmp-промо. Фикс: `neutral_fallback = [] if st == "dmp" else neutral_new` — для dmp только собственные B2B-examples агента; авто-слепки не тронуты.
- Статус: 🟡 частично подтверждено (прогон 2026-07-14, job 3f521254031a): ошибка «процент промо не совпадает» УШЛА (гейт больше не режет PCT-промо dmp) ✅. НО промо всё равно НЕ привязано (0/14) — теперь падает НИЖЕ, на Grid-создании: `DefectIds.MUST_BE_NULL` на amount/unit/prefix. Гейт-фикс верен, но недостаточен для «промо привязано». Следующий блокер = новая запись `DMP_PROMO_GRID_MUST_BE_NULL`.
- НЕ помогло ранее: — (первая правка гейта; сам гейт исправлен, блокер сместился на Grid-add).

### DMP_PROMO_GRID_MUST_BE_NULL — Grid отклоняет автопромо dmp: amount/unit/prefix MUST_BE_NULL (2026-07-14)
- Симптом: ПИЛОТ porg-mushirne (job 3f521254031a): `result.promo = "…grid отклонил автопромо: [{\"code\":\"DefectIds.MUST_BE_NULL\",\"path\":\"promoExtensions[0].amount\"},{…unit},{…prefix}]"`. Промо не создано в библиотеке → 0/14 кампаний с промо.
- Где: `automation_runtime.py::_seed_and_create_slepok_promo` :3713-3716 (`PromoClient(...).add(type=promo["type"], amount=promo["amount"], unit=promo["unit"], prefix=promo["prefix"], …)`). Preset dmp `ai_agents.py` AGENT `promo`: `type=PROFIT, unit=PCT, prefix=TO, amount 100-150`. `_promo_from_slepok` :3602 ВСЕГДА ставит `amount = random.choice(steps)` (non-None).
- Root-cause: для типа промо, который шлётся в Grid, схема требует amount/unit/prefix = null (text-only промо), а код всегда передаёт их непустыми (dmp preset PROFIT/PCT/amount). `content_pct=None` у B2B-контента dmp → ветка :3692-3697 (пересчёт amount из контента) не срабатывает, но amount из preset остаётся. Grid `addPromoExtensions` рубит `MUST_BE_NULL`.
- Фикс (2026-07-14, direct_fixer): `automation_runtime.py::_create_account_promo_from_slepok` ~3698 — после ветки content_pct добавлен изолированный гейт `if st == "dmp": promo["amount"]=None; promo["unit"]=None; promo["prefix"]=None`. `PromoClient.add` пропускает None-поля (`if amount is not None` / `if unit` / `if prefix`) → в `promoExtensions[0]` нет amount/unit/prefix → text-only промо (type=PROFIT из preset, посыл в description). `st` = `ctx.get("site_type")` из `{**account, "site_type": site_type}` (create_set_promo.py:58). `_promo_preview` amount=None-safe (promo_gen.py:237 guard). Авто-слепки со скидкой (site_type≠dmp) не затронуты — amount идёт как раньше.
- Обоснование типа: live-ответ Grid прямо потребовал amount/unit/prefix=null ДЛЯ отправленного type=PROFIT → в этой Grid-схеме PROFIT (как отправлен) = text-only. Официальные доки (`docs/efficiency/promotion.md`): text-only типы без «Размера» — «Подарок»/«Бесплатно»/«Рассрочка», числовые — «Скидка»/«Выгода»/«Кешбэк»; для «до N% лидов» dmp «Размер» не применим (150% вне PCT-диапазона 0-100). Держим type=PROFIT + null-поля (минимальная правка под live-ошибку), а не смену enum.
- Статус: ✅ ПОДТВЕРЖДЕНО ВЖИВУЮ (2026-07-14). Text-only промо dmp (type=PROFIT + amount/unit/prefix=null) СОЗДАНО в 3 боевых кабинетах y-direct-victory через `PromoClient.add`: need-leads.ru/porg-r7ro6tei → id **1976929**, need-lead.ru/porg-jh2si7rh → id **1976930**, needleads.ru/porg-as46rje6 → id **1976931**. Все верифицированы v5 `promotions.get`: `Type=PROFIT, Name="Выгода до 150% лидов", Description="до 150% лидов", Amount=null, AmountUnit=null`. Grid НЕ вернул `MUST_BE_NULL` и НЕ потребовал amount → риск-гипотеза «PROFIT не text-only» СНЯТА: PROFIT+null = валидное text-only промо. Идемпотентность подтверждена (повторный запуск → skip по существующему). ⚠️ Использован детерминированный `description="до 150% лидов"` НАПРЯМУЮ (не random `_promo_from_slepok`) т.к. dmp-библиотека `direct_slepok_content` засижена авто-фразами («на новые авто»/«на все модели») — random-пик мог нарушить «без авто-фраз». Осталось (отдельно): live-привязка text-only промо к кампаниям (`updateCampaignsPromoExtension`) — здесь создана только сущность в библиотеке. **ПРИВЯЗКА ТЕПЕРЬ ТОЖЕ ПОДТВЕРЖДЕНА:** контрольный пилот 2026-07-14 (job fcd1d01c0d93, porg-mushirne) — промо id **1976920** создано в библиотеке (v5-подтверждено) И привязано live к 8/8 tp2-кампаниям (`_read_unified_campaign_update_payloads` → `promoExtensionId=1976920`), без MUST_BE_NULL. tp6/tp7 (UAC/МК) промо не несут (не применимо).
- НЕ помогло ранее: гейт-фикс `DMP_PROMO_PCT_GATE_FALSE_REJECT` (убрал ложный reject по %, но промо падало ниже на Grid-add).

### DMP_CALLOUTS_NOT_PUSHED — уточнения dmp не заливаются в аккаунт и не привязываются (2026-07-14)
- Симптом: ПИЛОТ porg-mushirne (job 3f521254031a): 0/14 кампаний с callouts; **библиотека callouts аккаунта = 0** (`GridClient.get_callouts`=0); `result.callouts=null`. 20 засиженных dmp-callouts (`direct_slepok_callouts`) в аккаунт НЕ попали.
- Где: путь заливки/привязки callouts при создании набора (callout-создание в библиотеке аккаунта + `set_campaign_callouts`). result.callouts=null → шаг вообще не отработал для dmp headless-набора.
- Root-cause (установлен 2026-07-14): headless-путь (test_client / воркер без UI) шлёт `body["callouts"]` ПУСТЫМ — уточнения в попапе набора никто не отмечал. `normalize_create_set_input` → `_input["callouts"]=[]` → `run_create_set_precreate(callouts=[])` → `execute_precreate_assets` гейт `if callouts and v5_call and token` НЕ входит → `v5_ensure_callout_pool` не зовётся → библиотека аккаунта пуста → `precreated_callout_ids=[]` → tp1–tp5 привязывают 0. UI-путь работал (пользователь отмечал → callouts непусто).
- ФИКС (2026-07-14, `create_set_orchestrator.py` ~118, после `callouts=_input["callouts"]`): если `callouts` пуст И `agent` даёт валидный slepok (`_selected_slepok_key`) — авто-подтяжка из `public.direct_slepok_callouts WHERE slepok=%s ORDER BY usage_count DESC,accounts_count DESC,text` (тот же SELECT, что UI-роут `/api/slepok_callouts` routes_pack.py:38-42; коннект `direct_repository.victory_conn`), нормализация тем же `normalize_callouts`, кап `_CALLOUT_PER_CAMPAIGN_CAP`=8. Только slepok набора (чужие не заливаем). try/except — подтяжка не роняет набор. Backup `.editbak.20260714_103510`, py_compile OK.
- Донос до КАМПАНИЙ: tp1/tp2/tp3/tp5 (текст/галерея) — precreate наполняет библиотеку → `precreated_callout_ids` → grid-finalize `inheritableCallouts` (ПОКРЫТО). **tp6/tp7 (МК/Товарка) НЕ покрыто:** идут через Wizard `create_master_campaign` → `build_payload` (campaign.py:1528), в котором ПОЛЯ callouts НЕТ вообще, а `MasterCampaignSpec` не имеет атрибута callouts; post-create grid-callout-attach в master_product-пути отсутствует. Подтверждение отдельным фактом (2026-07-07 psm5h7q6): «UAC/tp7 пропущены — не поддерживают уточнения». → tp6/tp7-привязка уточнений — ОТДЕЛЬНЫЙ неподтверждённый механизм (нужен HAR wizard-callouts ИЛИ live-проверка `set_campaign_callouts` на universal-МК), не фикшу вслепую (правило №0).
- Статус: ✅ ПОДТВЕРЖДЕНО (контрольный пилот 2026-07-14, job fcd1d01c0d93, porg-mushirne headless). Библиотека callouts аккаунта = **8** (`GridClient.get_callouts`>0, было 0). Все **8/8 tp2**-кампаний несут по 8 callouts (`inheritableCallouts.calloutIds`, live). Авто-подтяжка из `direct_slepok_callouts` при пустом body сработала. tp6 (6 МК/UAC) callouts не несут — не применимо (известно).
- НЕ помогло ранее: —

### DMP_TP2_AUTOTARGET_REPAIR_404_BLOCK — узкий автотаргет tp2 НЕ применяется: пост-create репейр падает 404 на незарегистрированных dmp-аккаунтах (2026-07-14)
- ⚠️ ПЕРЕСМОТР 2026-07-14 (была помечена `DMP_TP2_WRONG_AUTOTARGET_FALSE_POSITIVE` / «ложняк верификатора» — НЕВЕРНО). Диагностика по живым job показала РЕАЛЬНЫЙ дефект: широкий автотаргет tp2 (все 5 категорий + 3 бренда, включая конкурентов) — это НЕ норма dmp, а недокрут узкого профиля `EXACT_V2_MARK`/`WITHOUT_BRAND`, который должен ставить пост-create авто-репейр.
- Симптом: ПИЛОТ porg-mushirne/need-number.ru (job 3e8a72fdae44, dmp, вся Россия): все 14 РК создались, live_verification=**fail** — 8×WRONG_AUTOTARGET на всех 8 tp2 («нужен EXACT_V2_MARK/WITHOUT_BRAND»). Live-профиль групп = полный дефолт (все 5 кат + 3 бренда, ветка `grid_create.py:472 elif autotargeting`).
- Где: `repair_executor.py::execute_keywords_repair` (автотаргет-ветка репейра, транспорт cookie+Grid `UpdateUnifiedAdGroups`). tp2/tp4/tp5 (`_SEARCH_TPS`).
- Root-cause: узкий автотаргет ставится НЕ атомарно при создании, а пост-create авто-репейром. При создании групп с ключами Яндекс сбрасывает автотаргет на широкий дефолт; репейр должен вернуть узкий, но `repair_executor.py:659-661` (было) на незарегистрированных dmp-аккаунтах падал хард-404 «аккаунт не найден в БД» → узкий профиль не применялся → группы оставались широкими. `acc` из БД в этой функции нужен был ТОЛЬКО для этого гейта + agency-fallback; сам автотаргет-репейр требует лишь куку (`cmc.build_client`) + Grid, строку аккаунта из БД — нет.
- Решение (2026-07-14, scoped, `repair_executor.py:659`): хард-404 снят → `acc = deps.account_ctx(login) or {}`, agency резолвится как прежде `ctx/body/acc.get("agency")`. Отсутствие аккаунта в БД больше НЕ прерывает автотаргет-репейр — он идёт по куке+Grid. Для ЗАРЕГИСТРИРОВАННЫХ аккаунтов поведение идентично (acc находится, agency тот же). Реальная невозможность (нет куки/agency) по-прежнему ловится ниже как 502 (`build_client` бросает → «не удалось подобрать рабочую куку») — не маскируется под успех. Идемпотентность цела: уже-узкие группы пропускаются (`:699`), группы с retargetings/bidModifiers пропускаются (`:691`). Backup `.editbak.20260714_091321`, py_compile OK.
- Статус: ✅ ЗАКРЫТО ОБХОДОМ (масштаб 2026-07-14). Сам пост-create репейр-путь узкого автотаргета через `UpdateUnifiedAdGroups` оказался тупиковым (404 снят, но запись падала валидацией дублей — см. `DMP_TP2_AUTOTARGET_UPDATE_DUP_KEYWORDS`). Итоговое решение: узкий автотаргет ставится **АТОМАРНО при Grid `AddUnifiedAdGroups`** (profile `search_tp2`), репейр НЕ нужен. На всех 4 акках масштаба автотаргет tp2 = 68/68 УЗКИЙ из create. **Урок (НЕ помогло ранее):** пост-патч автотаргета `UpdateUnifiedAdGroups` на свежих Grid-группах хрупок (лаг репликации + round-trip дублей) — ставить атомарно при Add.
- НЕ помогло ранее: (1) пометка «ложняк верификатора» (широкий=норма dmp — НЕВЕРНО). (2) снятие 404 в repair_executor (2026-07-14) — репейр запустился, но write отклонён Grid-валидацией `MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS` (дубли ключей в payload). Снятие 404 — необходимо, но НЕ достаточно.

### DMP_TP2_AUTOTARGET_UPDATE_DUP_KEYWORDS — узкий автотаргет не пишется: UpdateUnifiedAdGroups рубит ВЕСЬ батч из-за дублей ключей в round-trip payload (2026-07-14)
- Симптом: ПИЛОТ porg-mushirne (job 3f521254031a): delayed-repair `2f87ae5f1c42` гоняет keywords_repair на 8 tp2, но `auto_repair_full.failed[keywords_repair].failed_campaigns` = `Grid UpdateUnifiedAdGroups (autotarget): validation [{"code":"CollectionDefectIds.Gen.MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS","path":"updateAdGroupItems[8].keywords[5]"}, …]`. Репейр помечает группы `applied:true, fixed_autotarget:true` (68 «repaired»), но `ok:false`, executed=0, remaining=8; статус repair → `partial`. Live: 0/68 узких, все 68 широкие (WRONG_AUTOTARGET держится).
- Где: `repair_executor.py::execute_keywords_repair` :752-766 (`build_update_item(grp, keywords=final_kw, relevance_match=target_rm)`) → :792-796 `grid.update_unified_adgroups(write_items)`.
- Root-cause: когда чинится ТОЛЬКО автотаргет (need_at=True, need_kw=False — в группе уже есть ключи), `final_kw = list(grp.get("keywords"))` = существующие ключи группы round-trip'ятся в payload апдейта. В этих ключах есть ДУБЛИ фраз → `UpdateUnifiedAdGroups` валидирует `keywords` как коллекцию без дублей и **рубит ВЕСЬ батч 68 групп** одной ошибкой → ни одна группа не сужается. Одна дублирующая фраза в одной группе валит апдейт всех.
- Гипотеза фикса (НЕ реализовано, зона direct_fixer, требует решения Семёна): (а) дедуп `final_kw` перед `build_update_item` (сохранять порядок, `dict.fromkeys`); ИЛИ (б) при чистой автотаргет-правке (need_kw=False) слать relevanceMatch-апдейт БЕЗ массива keywords вообще (ключи и так живут отдельно, залиты через AddKeywords) — не round-trip'ить их; ИЛИ (в) писать группами (per-cid), чтобы дубль в одной не рушил остальные.
- Решение (2026-07-14, `repair_executor.py`, выбран путь (б)+(в) — чистейший): (1) `build_update_item(grp, keywords=[], relevance_match=target_rm)` — в payload автотаргет-апдейта keywords больше НЕ round-trip'ятся. Обосновано тем, что UpdateUnifiedAdGroups — **подтверждённый no-op для ключей** (repair_auto.py:57/198, repair_gate.py:160: ключи нельзя ни добавить, ни удалить этим апдейтом; заливка только AddKeywords) → пустой массив НЕ стирает живые ключи группы, но снимает `MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS` (пустая коллекция без дублей). (2) `update_unified_adgroups` вызывается **per-cid** (батчинг write_items по campaign_id из intents) с try/except на каждую кампанию → валидационная ошибка одной группы рубит только её кампанию (~8 групп), остальные 60/68 сужаются. Упавшие cid → в `failed` + их gid в `at_failed_gids` (не считаются applied) → `ok=False`, честный partial, retry по reschedule. need_kw-путь НЕ тронут (ключи по-прежнему через AddKeywords из `_kw_list`). Backup `.editbak.20260714_103624`, py_compile+pyflakes OK. Морфикс 404 (:659) не затронут.
- Статус: ✅ ПОДТВЕРЖДЕНО МАСШТАБОМ (2026-07-14, 4 акка). `MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS` УШЁЛ — Grid больше НЕ рубит батч на дублях (фикс keywords=[]/per-cid). Автотаргет tp2 = **68/68 УЗКИЙ** (EXACT_V2_MARK/WITHOUT_BRAND) на всех 4 акках, АТОМАРНО из create (Grid AddUnifiedAdGroups profile `search_tp2`, `create_set_text_builders.py:50-86`) — репейр не задействован (и не нужен). Ранний контрольный пилот fcd1d01c0d93 показал побочный 404 репейра — снят переходом на единый Grid-транспорт ключей (см. `DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT`).
- НЕ помогло ранее: снятие 404 (`DMP_TP2_AUTOTARGET_REPAIR_404_BLOCK`) — запустило репейр, но не устранило dup-keywords rejection.

### DMP_TP2_KEYWORDS_LOST_MIXED_TRANSPORT — ключи tp2 не закрепляются: v5 keywords.add на свежих Grid-группах = фантом + репейр пересчёта блокирован 2-м 404 (2026-07-14)
- Симптом: контрольный пилот porg-mushirne/need-number.ru (job fcd1d01c0d93, dmp). Все 14 РК создались (0 failed, без 152), автотаргет узкий 68/68 — НО **ВСЕ 68 tp2-групп имеют 0 ключей** (авторитетно: `groups_for_edit.keyword_count`=0 и `GridReadClient.campaign_content_counts.keywords_count`=0, суммарно 0). Прошлый пилот (v5-путь) держал 2386.
- Где: create — `create_set_text_builders.py::_fill_search_groups_batch` Фаза 1 (Grid `AddUnifiedAdGroups`, узкий автотаргет атомарно) + Фаза 2 (`_v5_call("keywords","add",...)` на тех же группах). repair — `repair_executor.py::execute_keywords_repair` need_kw-ветка :707-719 (`deps.group_keywords_context`).
- Root-cause (двойной, оба тянут ключи в 0):
  1. **Смешанный транспорт при создании.** Группы теперь создаются через Grid AddUnifiedAdGroups (чтобы поставить EXACT_V2_MARK/WITHOUT_BRAND АТОМАРНО — фикс WRONG_AUTOTARGET). Ключи же льются Фазой 2 через **v5** keywords.add на этих СВЕЖИХ Grid-группах. В пилоте `build` отчитался `keywords: 494` (Идентификация), errors=[] — v5 вернул AddResults с Id — но LIVE эти ключи 0. Классический лаг репликации Grid→v5: keywords.add рапортует успех на группе, которую v5 ещё не видит консистентно → ключи-фантомы, не закрепляются. Это ровно предупреждение ERRORS_JOURNAL «пост-патч на свежих v501/Grid-группах ХРУПОК из-за лага репликации».
  2. **Репейр не спасает — 2-й, не исправленный 404.** delayed-repair `6ee7f55ebf9a` увидел kw=0 (need_kw=True) на 40/40 групп → пошёл в пересчёт ключей `deps.group_keywords_context(login,...)` → тот бросает «аккаунт porg-mushirne не найден в БД» (хард-404 в keyword-recompute, ОТДЕЛЬНЫЙ от исправленного на `repair_executor.py:664`). applied=0, remaining=8, status=partial. Ключи не долиты.
- Гипотеза фикса (НЕ реализовано, зона direct_fixer; сначала ПОЙМИ механику): (а) заливать ключи tp2 ТЕМ ЖЕ транспортом, что и группы — Grid `AddKeywords` сразу после Grid AddUnifiedAdGroups (единый транспорт, без v5-фантома), ЛИБО (б) гарантировать видимость группы v5 перед keywords.add (poll adgroups.get по id до успеха / небольшой ретрай-барьер), ЛИБО (в) вернуться к v5 adgroups.add + пост-патч автотаргета — но это возвращает исходный WRONG_AUTOTARGET (не годится). Параллельно снять 2-й хард-404 в keyword-recompute-ветке (group_keywords_context не должен требовать аккаунт в БД для незарег. dmp-аккаунтов, как уже сделано для автотаргет-ветки), чтобы репейр мог долить ключи как safety-net.
- Статус: ✅ ПОДТВЕРЖДЕНО ЖИВЫМ ПРОГОНОМ (контрольный пилот 2026-07-14, job **d7abf8f70ca2**, porg-mushirne/need-number.ru, dmp). Путь (а): Фаза 2 `_build_tp2_adgroups` (`create_set_text_builders.py:93-115`) переведена с `_v5_call("keywords","add",...)` на Grid `_gcl2.add_keywords([{adGroupId,keyword}])` — ТОТ ЖЕ клиент/транспорт, что Фаза 1 создала группы. **Ключи ВЕРНУЛИСЬ: LIVE=2386, 0 пустых групп из 68** (`groups_for_edit.keyword_count`=0 zero + `campaign_content_counts.keywords_count` суммарно 2386). **build==live: 2386==2386** (Идентификация 480, Маркетинг 228, Околотематич 39, Расширенное 446 ×2 пары; errors=[] на всех 8) — фантомов НЕТ, лаг Grid→v5 устранён. items-формат Grid (`adGroupId` str, `keyword`), `_kw_clean(...,200)` дедуплит+капит ≤200/группу. Причина закрепления: Grid видит СВОИ только что созданные группы без лага (эталон cookie-путь create_full:624-638). Фаза 1 (узкий автотаргет search_tp2) НЕ тронута → автотаргет остался **68/68 EXACT_V2_MARK+WITHOUT_BRAND** (регресс не сломал узость). Репейр НЕ понадобился (auto_repair=null) — ключи легли атомарно при create. py_compile OK. Backup `.editbak.20260714_115931`. Параллельный 2-й 404 в keyword-recompute репейра (safety-net) остался НЕ тронут — но на чистом create-пути не срабатывает, т.к. репейр не вызывается.
- НЕ помогло ранее: v5-путь создания (adgroups.add+keywords.add одним транспортом) ключи ДЕРЖАЛ (2386), но давал ШИРОКИЙ автотаргет (WRONG_AUTOTARGET). v5 keywords.add на СВЕЖИХ Grid-группах = ключи-фантомы (LIVE=0). Смешивать транспорты (Grid группы + v5 ключи) — НЕ повторять.

### DMP_IMAGES_TRUNCATE_BEFORE_PHASH_DEDUP — в spec МК/ТК dmp доезжало 2 картинки из 50 (усечение ДО дедупа) (2026-07-14)
- Симптом: dmp per-domain МК (tp6) в кампанию отдавала 2 картинки из ~50 доступных доменных, хотя должно быть ≥5 distinct.
- Где: `automation_runtime.py::_creative_images_for_ct` (не-авто ветка ~2509) → `create_set_master_product.py:582` → `campaign.py::collect_image_files`/`_image_phash` (pHash hamming≤10).
- Root-cause: неверный ПОРЯДОК «обрезали до 12 → дедуп». Доменный набор 50 сначала резался до `_candidate_image_limit=12` (`_creative_images_for_ct` `[:limit]` + `create_set_master_product.py:582` `[:12]`), и только потом `collect_image_files` гнал pHash-дедуп на этих 12 почти-одинаковых баннерах → выживало 2. Если дедупить ВЕСЬ набор из 50 → 7 distinct (need-number.ru).
- Решение (Путь A, scoped, ТОЛЬКО `automation_runtime.py`, не-авто ветка): pHash-distinct прогоняется на ВСЁМ доменном наборе (путь→md5→`cmc._image_phash` hamming≤10 — та же функция и порог, что в `collect_image_files`) ДО усечения, возвращаются первые `limit` DISTINCT. `campaign.collect_image_files` НЕ тронут (авто tp6/tp7 и tp1-tp5 идут мимо не-авто ветки — регресс исключён). Порог 10 НЕ понижен, чужие домены не подмешаны (тег `dmp:<домен>` сохранён).
- Статус: ✅ подтверждено прогоном 2026-07-14 (ПИЛОТ porg-mushirne/need-number.ru, job 3e8a72fdae44). Live-чтение 6 созданных МК (tp6, UacReadClient.campaign_detail → contents type=image): **ВСЕ 6 МК = 5 distinct картинок** (МК Авто cpc/cpa, МК Ключи cpc/cpa, МК Конкуренты cpc/cpa — по 5/5). Раньше было 2. Фикс порядка дедуп/усечение работает на боевом domain need-number.ru. Ранее (до прогона): end-to-end трейс на LXC101 read_slepok_images(dmp,tp6,ct0000,'dmp:need-number.ru')=50 → OLD `[:12]`→distinct=**2**; NEW=7 distinct→image_limit **5**.
- ⚠️ ДАННЫЕ (не код): **need-lead.ru** имеет лишь **2** визуально-distinct баннера среди 50 (50 unique-md5/размеров, но pHash-hamming к file0 = 0/2/4/8 — почти-идентичны). Код дедупит весь набор корректно; 5 distinct там недостижимо БЕЗ понижения порога (запрещено) или подмешивания чужих доменов (запрещено). → контент-задача: доснять distinct-баннеры для need-lead.ru, ЛИБО решение Семёна. Не баг фикса.
- НЕ помогло ранее: — (первая правка порядка дедуп/усечение). Путь B (поднять оба окна усечения) не выбран: пришлось бы гейтить по домену в 2 файлах + прогонять 50 картинок downstream + магические окна; Путь A правит источник в 1 файле и не трогает collect_image_files (авто-путь).
- **ПЕРЕСМОТР 2026-07-14 (Семён): pHash-дедуп ДЛЯ dmp = ОШИБОЧНЫЙ, снят.** «⚠️ ДАННЫЕ» выше НЕ верно: 50 баннеров need-lead.ru — это шаблон «тёмный фон + РАЗНЫЙ текст», для Директа это РАЗНЫЕ объявления (он их принимает). pHash hamming≤10 ошибочно схлопывал их в «дубли» → в spec доезжало 2. Fix: для dmp/не-авто убран pHash-уровень, оставлен ТОЛЬКО path+md5 (байт-дедуп). См. новую запись `DMP_PHASH_COLLAPSES_DISTINCT_BANNERS`.

### DMP_PHASH_COLLAPSES_DISTINCT_BANNERS — pHash схлопывал разные dmp-баннеры одного шаблона в «дубли», в МК доезжало 2 из 50 (2026-07-14)
- Симптом: dmp per-domain МК (tp6) на need-lead.ru доезжало 2 картинки из 50, хотя все 50 визуально разные по ТЕКСТУ (шаблон «тёмный фон + разный оффер»). Директ такие принимает как разные объявления.
- Root-cause (подтв. Семёном): pHash hamming≤10 меряет СТРУКТУРУ изображения (низкочастотные DCT-коэф.), а не текст. Баннеры одного шаблона отличаются лишь текстом → pHash-hamming к file0 = 0/2/4/8 ≤10 → схлопываются в «дубль». Для лидоген-dmp это не дубли. pHash-порог понижать нельзя (сломает авто-защиту от клонов), поднимать окна усечения (Путь B) не помогает — схлопывает сам pHash.
- Решение (scoped, dmp/не-авто; авто-путь НЕ тронут):
  - `automation_runtime.py::_creative_images_for_ct` не-авто ветка (~2517-2549): убран pHash-уровень (`cmc._image_phash`/`kept_ph`), оставлен path→md5, возвращаем первые `limit` УНИКАЛЬНЫХ по содержимому.
  - `campaign.py`: новое поле `MasterCampaignSpec.visual_dedup: bool = True` (~1094); в `collect_image_files` (~1749/1782) gate `do_visual = visual_threshold>0 and spec.visual_dedup` — при False pHash пропускается (`ph = _image_phash(key) if do_visual else None`), md5-дедуп остаётся.
  - `create_set_master_product.py` (~665): в spec `visual_dedup=(_sk != "dmp")` — False только для dmp; авто-слепки → True (pHash-защита от клонов сохранена).
- Факт-прогон (LXC101, worker venv `/root/venv/bin/python3` c PIL 12.2.0/numpy 2.4.6, реальные файлы `/opt/neuro_content_local/kontent_oktyabr/dmp/tp6/ct0000/`): **need-lead.ru** СТАР(pHash) collect(50)→**2**→залив 2; НОВ(md5-only) creative md5-distinct(≤12)=12→collect(visual_dedup=False)=12→залив **5**. Прочие домены: need-number 7→5 (НОВ 5), need-leads 50→5 (НОВ 5), needleads 48→5 (НОВ 5). Изоляция: пересечение имён файлов need-lead∩need-number = **0**. Авто-путь: дефолт `visual_dedup=True` (pHash активен). Backup .editbak.20260714_084516 ×3, py_compile OK, md5 Mac==LXC101 ×3.
- Статус: ✅ ПОДТВЕРЖДЕНО ВЖИВУЮ (масштаб 2026-07-14). На ранее проблемном акке need-lead.ru/porg-jh2si7rh (job 5fc231936faa) все 6 МК несут по 5 distinct картинок; то же на остальных 3 акках (5×6). pHash больше не схлопывает разные dmp-баннеры в «дубли». Живая заливка в кабинет прошла.
- НЕ помогло ранее: Путь A (`DMP_IMAGES_TRUNCATE_BEFORE_PHASH_DEDUP`, дедуп всего набора ДО усечения) — НЕ решил need-lead.ru: pHash всё равно схлопывал 50→2 (корень был не в порядке усечения, а в самом pHash для текстовых баннеров). Понижение порога / подмешивание чужих доменов — ЗАПРЕЩЕНО (не пробовать).

### DMP_IMAGES_TP_PINNED_TP6 — dmp per-domain картинки резолвились только для tp6-кампаний, для tp≠tp6 пусто (2026-07-13)
- Симптом: у dmp per-domain (тег `dmp:<домен>`) МК/tp6 получали свои картинки, а другие tp кампании (tp2 и пр.) выходили без картинок — `_creative_images_for_ct` возвращал `[]`.
- Где: `automation_runtime.py::_creative_images_for_ct` (не-авто ветка ~2509). Вызов с `domain` только из `create_set_master_product.py:570/576` (`_img_domain=_norm_domain if _sk=="dmp" else ""`).
- Root-cause: манифест dmp-картинок (`image_slepki.txt`, строки `<rel>\tdmp:<домен>`) физически лежит ЕДИНОЖДЫ в `dmp/tp6/ct0000/` и общий на все tp. Но код читал `kp.read_slepok_images(site_type, tp, "ct0000", img_tag)` с ПЕРЕМЕННЫМ `tp` кампании → для tp≠tp6 бил в пустую `dmp/<tp>/ct0000/` → []. `ct` уже был захардкожен `ct0000` (ct-agnostic), а `tp` — нет.
- Решение: `automation_runtime.py` — в не-авто ветке `_img_tp = "tp6" if domain else tp`; читаем `read_slepok_images(site_type, _img_tp, "ct0000", img_tag)`. Гейт по `domain`: domain непустой ⟺ dmp per-domain (подтверждено `_img_domain = _norm_domain if _sk=="dmp" else ""`), поэтому прочие не-авто слепки (domain пусто) читают из СВОЕГО tp как раньше — их картинки хранятся в их tp-папках, пиннинг сломал бы. site_type для dmp = "dmp", путь `dmp/tp6/ct0000` совпадает с местом манифеста. (2026-07-13)
- Статус: 🟡 ждёт живого прогона dmp по tp≠tp6. Оффлайн-трейс на LXC101 (worker-env NEURO_PACK_MOUNT=/opt/neuro_content_local): domain=need-lead.ru → camp_tp=tp2 И tp6 оба read_tp=tp6 → 50 картинок need-lead.ru; без domain → read_tp==camp_tp (не пиннится, регресс не задет); контроль read_slepok_images(dmp,tp2,...)=0 vs (dmp,tp6,...)=50 = ровно исправляемый баг.
- НЕ помогло ранее: — (первая правка). Смежно НЕ трогалось: другие вызовы `_creative_images_for_ct` (`create_set_tp1_builders.py:696/1372/1545`, `create_set_text_builders.py:378`, `create_set_repairing.py:338`) зовут БЕЗ `domain` → для dmp дают img_tag="dmp" (без домена) → не матчат domain-теги → []. Сегодня не бьёт (dmp per-domain картинки идут только через `create_set_master_product` в tp6-МК). Протяжка domain в эти вызовы = отдельная правка (широкая), НЕ делалась.

### VICTORY_CONN_STALE_SSL_EOF_500 — content-editor 500 → фронт «Unexpected token '<' … not valid JSON» после простоя (2026-07-13)
- Симптом: во фронте content-editor `SyntaxError: Unexpected token '<', "<!doctype "... is not valid JSON` (Flask отдал HTML-страницу 500, `r.json()` упал). Проявлялось на `/direct/api/content-editor/accounts` («Обзор») и на «Проверить блокировку» после простоя аккаунта; днём те же ручки отдавали 200 (транзиентно).
- Где: content-editor (`direct-content.service` :5021), `direct_repository.py::victory_conn`. Общий read-only пул Victory (используют «Обзор», баланс, сверка цен, а для scoped-юзера ещё «Проверка блокировок» через `routes_content_editor._pairs_allowed`).
- Root-cause (подтверждён логом 13.07 20:50): Postgres на Victory молча роняет idle-соединение (SSL EOF), но `conn.closed` остаётся `0` → голая проверка `if conn.closed` проходит → первый реальный I/O (`conn.set_session(...)`) кидает `psycopg2.OperationalError: SSL SYSCALL error: EOF detected`. Ретрая/пинга не было → Flask 500 (HTML).
- Решение (`direct_repository.py::victory_conn`, 2026-07-13): выданное из пула соединение пингуется (`SELECT 1`) в `try/except psycopg2.OperationalError`; при ошибке мёртвый сокет закрывается (`pool.putconn(conn, close=True)`, НЕ возвращается в пул живым) и делается ОДИН ретрай на свежем `getconn()`; если и он падает — исключение пробрасывается (не глотаем). Здоровый путь не изменён (живое соединение → как раньше). Патч только в этой функции.
- Статус: ✅ подтверждено на LXC101 (2026-07-13): (1) healthy — accounts?status=__all__ → 200 JSON 1238 rows; (2) симуляция реального EOF — `pg_terminate_backend` СВОЕЙ victory-сессии за pooled-conn (closed остался 0!) → `victory_conn()` прозрачно переоткрыл (`RECOVERED`), 500 нет; (3) unauth live 401 (не 500), в journal после рестарта 0 `SSL SYSCALL`/500.
- check_blocks вердикт: для scoped-юзера DB-контакт есть (`routes_content_editor._pairs_allowed()` → `victory_conn()`), для full-access/admin — нет (выходит из `_pairs_allowed` до DB). ⚠️ ДОПОЛНЕНО 2026-07-13 (см. `CHECK_BLOCKS_PULL_LOCK_MISSING_500` ниже): у check_blocks была ВТОРАЯ, независимая причина 500, НЕ покрытая фиксом `victory_conn()` — незаконфигуренный pull-lock в content-процессе. Она била именно admin/full-access (кто DB-путь пропускает). «Отдельно чинить не надо» относилось только к DB-EOF-корню.
- НЕ помогло ранее: голая проверка `if conn.closed` (была до фикса) — серверный EOF не ловит (`closed==0`).

### CHECK_BLOCKS_PULL_LOCK_MISSING_500 — «Проверить блокировку» 500→HTML (`SyntaxError: Unexpected token '<'`), pull-lock не сконфигурен в content-процессе (2026-07-13)
- Симптом: кнопка «Проверить блокировку» (вкладка «Обзор» content-editor) под АДМИН-сессией → фронт `SyntaxError: Unexpected token '<'` = HTTP 500 (HTML вместо JSON). Баланс/«Обзор» работали. Отличие от `VICTORY_CONN_STALE_SSL_EOF_500`: воспроизводился СТАБИЛЬНО (не после простоя) и именно у full-access (кто минует `_pairs_allowed`→`victory_conn`).
- Где: `direct-content.service` :5021. `routes_content_editor.py:ce_check_blocks` → `account_service._check_blocks_response` → `_pull_begin("blocks",60.0)`.
- Root-cause (подтверждён живым логом LXC101, PID 1898783, 21:25): `account_service.py:25` объявляет `_pull_begin=_pull_end=_busy_response=_missing` (заглушка `raise RuntimeError("account_service dependency is not configured")`). Реальные реализации инжектятся ТОЛЬКО через `automation_runtime.configure(...)` (:3728-3731) — а это wiring процесса direct-CREATE. Процесс direct-CONTENT (`content_main.py`) `configure(...)` для pull-lock не вызывает вообще → `_pull_begin` навсегда `_missing` → любой эндпоинт content-editor, дёргающий его, падает 500. Баланс жив, т.к. `_do_balance` pull-lock не использует.
- Решение (2026-07-13, scoped, `account_service.py` — Вариант Б «самодостаточные дефолты»): в модуль добавлены `import threading, time` + per-process `_PULL_LOCK`/`_PULL_LAST`/`_PULL_OWNER` + дефолтные `_default_pull_begin`/`_default_pull_end`/`_default_busy_response` (зеркало `automation_runtime` :327/:341/:592), и строка 25 = `_pull_begin,_pull_end,_busy_response = _default_*` вместо `_missing`. direct-create по-прежнему перезаписывает их своим `configure()` (единый лок движка) → его поведение НЕ меняется; direct-content использует дефолты. `content_main.py` НЕ трогали (чистая граница, движок не импортируется). Прочие `_missing`-деп-ы (delete_drafts/assets/stopall) оставлены `_missing` — их эндпоинты в content-процессе НЕ регистрируются (`register_content_editor_routes` заводит только balance+check_blocks).
- Статус: ✅ подтверждено на LXC101 (2026-07-13, деплой Mutagen md5-сверен, py_compile+pyflakes чисто, рестарт direct-content, active PID 1908114). test_client с форс-admin-сессией на боевом коде: (1) check_blocks #1 → **200 application/json** `{"blocks":{"porg-asfbs7qe":false}}`; (2) повтор в пределах 60c → **429 application/json** cooldown (`_busy_response` отдаёт JSON, не 500); (3) balance регресс → 200 JSON; (4) журнал: до рестарта 8× `RuntimeError account_service dependency is not configured`+500, после — 0.
- НЕ помогло ранее: фикс `victory_conn()` (`VICTORY_CONN_STALE_SSL_EOF_500`) этот путь НЕ закрывал — тут иная причина (незаконфигуренный pull-lock, а не мёртвый DB-сокет); admin вообще не доходит до DB.

### MODEL_URL_BRAND_FALLBACK_WRONG_MODEL — ссылка объявления ведёт на ЧУЖУЮ модель марки (2026-07-13)
- Симптом: группа сегмента «Модели» (напр. `ct0042 — Changan UNI-T`, porg-psm5h7q6, autos-kemerovo.site) получала ссылку `/auto/changan/cs55/...` (первая модель Changan) вместо своей. Товарный сниппет тянул чужой товар (CS55, Ростов-на-Дону) — следствие той же неверной ссылки.
- Где: tp1/tp2 фид-путь, `create_set_feeds.py:_feed_url_for_model` (335); call-sites `create_set_tp1_builders.py:703,1511`, `create_set_text_builders.py:355`.
- Root-cause: `_feed_url_for_model` при отсутствии точного ключа модели падал на brand-fallback `urls.get(b.split()[0])` = URL первого оффера бренда (cs55). ГЛУБЖЕ (live-факт porg-psm5h7q6, feed-preview): `_grid_feed_offer_urls` строит ключи из `FeedOffersPreview`, а это ВЫБОРКА (sample), НЕ полный список офферов → оффер UNI-T (который в сыром XML фида ЕСТЬ, url `/auto/changan/uni-t/i/suv-5d?fid=yandex`) в sample НЕ попал → ключ `changan uni-t` не построился → brand-fallback. Системно: любая модель вне sample-preview промахивается.
- Решение (2026-07-13, вариант A, задеплоено): зеркало ПРАВКИ 5 из `_ad_price_for_brand` — параметр `no_brand_fallback=True` для сегмента «Модели» (`_feed_url_for_model:347`): нет точного/без-года ключа → `None`, НЕ брать бренд-оффер. При `None` builders уходят на формульный `_model_page_href` → `/auto/changan/uni-t` (верная модель/марка/домен). Прокинуто из 3 call-sites `no_brand_fallback=(_ct_segment(ct)=="Модели")`. Марки-сегмент не тронут (бренд-URL там легитимен).
- Решение (2026-07-13, вариант B, НЕ задеплоено): доливка ТОЧНЫХ url из raw XML авто-фида `<homepage>/yandex.xml` — новая `create_set_feeds.py::_auto_feed_urls(url)` (зеркало `_auto_feed_discount_prices`: requests.get с tries+backoff, итерация `<car>`, тег `<url>`, ключ = `_offer_price_keys(mark_id folder_id)` → «changan uni-t», первый url на ключ, свой кэш `_AUTO_FEED_URL_CACHE` TTL 20 мин, {} при сбое). В `_account_offer_urls` после Grid-цикла из sample: `for k,v in _auto_feed_urls(url).items(): out.setdefault(k, v)` — заполняет ТОЛЬКО пропущенные ключи (sample-covered модели не трогаются, приоритет sample). Теперь UNI-T есть в карте → `_feed_url_for_model` вернёт точный `/auto/changan/uni-t/i/suv-5d?fid=yandex`, а не формульный.
- Вариант A ОСТАЁТСЯ страховкой: модель, которой нет НИГДЕ (ни в sample, ни в raw XML) → ключ не построится → `_feed_url_for_model(no_brand_fallback=True)` вернёт None → builders на формульный `_model_page_href` (верная марка/модель/домен, без хвоста). `no_brand_fallback` и 3 call-site НЕ тронуты.
- Статус: 🟡 вариант A задеплоен 2026-07-13 20:33; вариант B код готов (create_set_feeds.py), НЕ задеплоено — ждёт синка на LXC101 + рестарта direct-create/-worker + живого прогона: Changan UNI-T → точный `/auto/changan/uni-t/i/suv-5d?fid=yandex` (не формульный, не cs55).
- Проверено (офлайн): py_compile + pyflakes (0 новых undefined в диапазоне правки). Трейс 3 кейсов: (a) UNI-T вне sample, есть в raw XML → ключ «changan uni-t» долит через setdefault → точный url; (b) covered-модель (в sample) → url НЕ перезаписан (setdefault пропускает существующий ключ); (c) модель без офферов нигде → карта пуста по ключу → вариант A формульный (страховка цела).
- НЕ помогло бы: полагаться на `FeedOffersPreview` для полноты офферов — это sample, не весь фид (доказано: UNI-T есть в XML, нет в preview 30 фидов). Вариант B закрывает это добором из raw XML.

### VIDEO_NO_POOL_AUDIT_IGNORES_SLEPOK_POOL — аудит видит только общий пул, слепковый игнорирует
- Симптом: для слепка со СВОИМИ видео (марки вне общего `_video_pool`, напр. `haval_ufa_si7rw3ua` — 21 mp4) аудит tp1 эмитит ЛОЖНЫЙ `VIDEO_NO_POOL` (info), хотя ролики в слепковом пуле есть; видео не довкладывается. Латентный, тот же класс, что tp3-#6 (пропущенная симметричная точка).
- Где: AUDIT — tp1 `campaign_spec_audit.py::_ct_has_pool_video` (773-808, вызов :899 в `_audit_tp1_adaptive`) и UAC `::_audit_uac_video_missing` (1287-1338, чек пула :1324).
- Root-cause: асимметрия create vs audit. СОЗДАНИЕ резолвит видео слепок-aware через `kp.videos_for_ct(login, ct, brand_hint)` (`kontent_pack.py:1141-1190`: сначала пул слепка `_slepki_data/<slepok>/videos/`, потом общий `_video_pool/<ct>` + brand-fallback) — `create_set_tp1_builders.py:122`, `create_set_master_product.py:579`. АУДИТ же звал pool-only `videos_pool_for_ct(ct)` БЕЗ `login` → слепковый пул невидим → у ct без записи в общем пуле пул «пуст» → ложный VIDEO_NO_POOL (tp1) / недо-детект UAC_VIDEO_MISSING (UAC).
- Решение (2026-07-13, `campaign_spec_audit.py`): tp1 — `_ct_has_pool_video(ct)` → `_ct_has_pool_video(login, ct)`, внутри `videos_pool_for_ct(ct)` → `videos_for_ct(login, ct, ...)` (login доступен в `_audit_tp1_adaptive`); call-site :899 обновлён. UAC — login доступен в `_audit_uac_video_missing`, но ct в контексте нет (только имя→brand_hint); зеркалю create-путь UAC (`videos_for_ct(login,c_ct) or videos_for_login(login)`): к pool-арму `videos_pool_for_ct("", brand_hint)` добавлен слепковый арм `videos_for_login(login)` — подавляем флаг ТОЛЬКО когда пусты ОБА.
- Проверено (офлайн): `py_compile` + `pyflakes` clean. Трейс на реальной `_ct_has_pool_video` со стабом kp: (a) haval-слепок ct0119 вне общего пула → NEW has=True → VIDEO_NO_POOL НЕ эмитится (OLD pool-only вернул бы [] → ложный NO_POOL); (b) pavlov ct вне обоих пулов → has=False → VIDEO_NO_POOL эмитится (regression сохранён); (c) базовый общий-пул кейс не сломан; UAC оба арма корректны.
- Статус: 🟡 код готов, НЕ задеплоено (ждёт синка на LXC101 + рестарта direct-create/-worker + живого прогона аудита на слепке haval со своими видео + pavlov-regression).
- НЕ помогло ранее: — (первая правка симметрии audit↔create по видео-пулу слепка).

### VIDEO_BRAND_FALLBACK_DEPENDS_ON_FEED_IMAGE — brand-level ct без фид-картинки НЕ фолбэчит на видео своих моделей (2026-07-13)
- Симптом: для brand-level ct сегмента «Марки» (ct0019 BAIC, ct0111 Haval) `videos_pool_for_ct(ct, brand_hint="")` возвращал `[]` → ложный `VIDEO_NO_POOL`, хотя у бренда есть ролики моделей (ct0020 BAIC, ct0112 Haval). Belgee ct0026 «повезло» — работал. Бьёт callers, которые НЕ передают brand_hint (аудит `_ct_has_pool_video`, UAC-арм `videos_pool_for_ct("", brand_hint)`, `videos_for_ct(login, ct)`-фолбэк без hint).
- Где: `kontent_pack.py::videos_pool_for_ct` brand-резолв (~1274-1290), опосредованно `videos_for_ct` (:1199 фолбэк на пул).
- Root-cause: `brand_word` собирался ТОЛЬКО из `feeds_ct_model()[ct]` (карта строится из имён ФИД-КАРТИНОК `_image_store/feeds`) → если картинки под brand-level ct нет, `feeds_ct_model()[ct]=None`; при пустом `brand_hint` `brand_word=''` → `if brand_word:` False → `return []`, brand-fallback не запускался. Т.е. фолбэк зависел от СЛУЧАЙНОГО покрытия фид-картинок, а не от справочника марок. Belgee ct0026 работал лишь потому, что случайно существует фид-картинка `ct0026_..._Belgee_X50` → `feeds_ct_model()[ct0026]='Belgee X50'` → `brand_word='belgee'`.
- Решение (2026-07-13, `kontent_pack.py::videos_pool_for_ct`, scoped): добавлен 3й источник `brand_word` — справочник марок `_ag_part1_map()` из `campaign_naming` (ct→'Марка Модель' из `public.gsheet_naming` ag_part1), ТОЛЬКО когда И `feeds_ct_model()[ct]` пусто, И `brand_hint` пуст: `ref=_ag_part1_map().get(ct); brand_word=ref.split()[0].lower()`. Первые 2 источника (feeds_ct_model, brand_hint) — приоритет, не тронуты. Ленивый импорт в try/except (kontent_pack — leaf-модуль без DI; если БД/DI недоступны в процессе → как было, `[]`). В create/worker процессе campaign_naming конфигурится через `automation_runtime` (:1625).
- Статус: 🟡 фикс на Mac (Mutagen синкнул на LXC101), НЕ задеплоено (Семён деплоит — рестарт direct-create/-worker; живой прогон tp1/UAC на brand-level ct без фид-картинки).
- Проверено на LXC101 (2026-07-13, worker-env `NEURO_PACK_MOUNT=/opt/neuro_content_local`, `/root/venv/bin/python3`, campaign_naming сконфижен через import `automation_runtime`): ресолвер `_ag_part1_map()[ct0019]='BAIC'`, `[ct0111]='Haval'`, `[ct0026]='Belgee'`. NEW: `videos_pool_for_ct("ct0019","")`→2 файла (ct0020_01/02.mp4), `("ct0111","")`→2 (ct0112_01/02), `("ct0026","")`→2 (ct0027_01/02, регресс сохранён), `videos_for_ct("porg-asfbs7qe","ct0019")` без hint→2 (ct0020). Дифф на целевой метрике: OLD `brand_word` для ct0019/ct0111 = `''` → fallback SKIP → `[]` (ложный NO_POOL); ct0026 OLD = `'belgee'` (случайная фид-картинка) → работал. Модельные ct бренда резолвятся в `feeds_ct_model` первым словом `baic`/`haval` (ct0020='BAIC BJ40', ct0112) → сравнение с `brand_word` совпадает. py_compile+pyflakes чисто (единственный pyflakes-варн — предсуществующий redefine `json` :1512, вне правки).
- НЕ помогло ранее: — (первая правка; смежная `VIDEO_NO_POOL_AUDIT_IGNORES_SLEPOK_POOL` закрывала асимметрию audit↔create по СЛЕПКОВОМУ пулу, но brand-fallback ОБЩЕГО пула так и оставался завязан на фид-картинку — этот фикс её убирает).

### 🔴 ПРОГОН porg-asfbs7qe (2026-07-13) — 10 ошибок с живого создания (Семён)

> Батч ошибок с ручного прогона. Триаж по домену; root-cause/фикс — по ходу. Статус каждой обновлять после правки+прогона.

#### 1. UNITS_CLIENT_NOT_AGENCY — использованы баллы КЛИЕНТА, а не агентства
- Симптом: при создании РК списаны/использованы units клиента вместо агентских (главпоток). Скрин.
- Где: домен ENGINE — выбор источника units при вызове API v5/Grid. Файлы-кандидаты: campaign.py / yandex_gateway / units-handling в create_set_*.
- Root-cause: TBD (проверить, откуда берётся Client-Login / units при операции).
- Статус: 🔴 обнаружено прогоном, root-cause в работе.

#### 2. NO_COOKIE_FALLBACK_ON_UNITS_EMPTY — нет добивки по куки при нехватке баллов
- Симптом: если баллов нет — создание НЕ переключается на куки-докрутку (152/баллы), просто не добивает.
- Где: ENGINE — fallback units→cookie. account_service.py (self-probe кук), докрутка-механизм.
- Статус: 🔴 root-cause в работе.

#### 3. CPA_BUDGET_RULES_IGNORED — глоб. правила бюджета для оплаты за конверсию игнорируются
- Симптом: кампания с оплатой за конверсию (CPA) создана без учёта глобальных правил бюджета — подставляется другое число. Скрин.
- Где: ENGINE — `create_set_plan.py:317-318` helper `_bud(pay)`. Пишется в `item["budget"]` для ВСЕХ per-pay tp: tp4 (:588), tp2 (:660, :692), tp6/tp7 master/product (:837).
- Root-cause: `rs` = глобальные правила из `public.direct_automation_rules` по (site_type, city), ключи `{cpa, budget, cpc_cpa, cpc_budget}` (`_rule_sets`, :90-119). Контракт (:315 `cpa, budget = rs["cpa"], rs["budget"]`; `resolved_budget=budget` :858, read-only справка формы): для CPA бюджет = `rs["budget"]`. Но `_bud` в CPA-ветке возвращал `rs["cpa"] * 10`, а не `rs["budget"]` → глоб. правило бюджета игнорировалось, подставлялось cpa×10 (с дефолтами: 20000 вместо 5000). CPC-ветка (`rs["cpc_budget"]`) корректна. Downstream-фолбэки `create_set_text.py:56` и `create_set_master_product.py:589,652` (`... else rs["budget"]`) — мёртвый код: primary `it["budget"]` уже заполнен неверно, фолбэк не срабатывает.
- Решение: `create_set_plan.py:318` — `rs["cpa"] * 10` → `rs["budget"]` (один пойнт, минимальная правка). Downstream-фолбэки НЕ тронуты (не нужны после фикса, но не мешают). 2026-07-13.
- Проверено (офлайн): `python3 -m py_compile create_set_plan.py` OK; трейс `_bud("cpa")` → `rs["budget"]` (дефолт 5000, DB 8000 — match контракта), `_bud("tcpa")` → `rs["cpc_budget"]` не изменилось.
- Статус: 🟡 код готов, НЕ задеплоено (ждёт синка на LXC101 + рестарта direct-create/-worker + живого прогона CPA-кампании).
- НЕ помогло ранее: — (первая правка; сигнатура была 🔴 в триаже, root-cause найден отдельным агентом).

#### 4. NAME_FEED_NOT_IN_CAMPAIGN — в названии кампании фид, которого в кампании нет
- Симптом: имя РК ссылается на фид, отсутствующий в самой кампании. Скрин.
- Где: ENGINE — формирование имени vs реально прикреплённый фид. create_set_feeds.py, naming.
- Статус: 🔴 root-cause в работе.

#### 5. FEED_WRONG_FIRST_NOT_SCRIPT — взят ПЕРВЫЙ фид в аккаунте вместо нужного по скрипту
- Симптом: нужного Яндекс-фида нет в аккаунте; вместо выбора корректного по скрипту взяли первый попавшийся. Скрин.
- Где: ENGINE — резолвер фида. create_set_feeds.py (выбор feed_id/feed_role).
- Статус: 🔴 root-cause в работе.

#### 6. CREDIT_IN_DEFAULT_TEXT_PRODUCT — «кредит» в тексте по умолчанию у товарных/каталожных
- Симптом: в default-тексте товарных (tp7/ShoppingAd) и каталожных объявлений присутствует «кредит» — нельзя. Скрин.
- Где: CONTENT — «текст по умолчанию» для product/listing. Реально: `create_set_feed_builders.py::_create_tp3_single` (НЕ ai_agents.py — см. полную запись ниже).
- Root-cause (доказан 2026-07-13): tp3 «Товарная галерея» ставил ShoppingAd default text = `data["default_text"]`, читаемый из `direct_slepok_content` texts (кампанийные тексты с кредитным углом), тогда как брат tp5 уже был пофикшен (R2-8 2026-07-10) на единый credit-free `SHOPPING_DEFAULT_TEXT`. tp3 пропустили — асимметрия.
- Статус: 🟡 фикс на Mac (tp3 → SHOPPING_DEFAULT_TEXT), py_compile OK. Полная запись — `CREDIT_IN_DEFAULT_TEXT_PRODUCT_TP3` ниже.

#### 7. HEADLINE_CHAR_BUDGET_UNDERUSE — много свободных символов
- Симптом: заголовки/тексты не добирают символьный бюджет (много места пустует). Скрин.
- Где: CONTENT — генерация заголовков. Промпт `ai_agents.py::build_titles_messages` + сборка `assemble_campaign._pad`.
- Статус: 🟡 LLM-подход реализован (2026-07-13) — см. `HEADLINE_CHAR_BUDGET_UNDERUSE_TITLES` ниже. Ждёт live-прогона.

#### 8. NO_VIDEO_ATTACHED — нет видео
- Симптом: в созданных РК нет видео (должны цеплять из видео-пула). Скрин.
- Где: ENGINE/CONTENT — видео-пул. kontent_pack.py (_video_pool), attach в объявление.
- Статус: 🔴 root-cause в работе.

#### 9. SLEPOK_MINUS_MISSING_ONLY_GLOBAL — минус из глоб.правил есть, из слепка НЕТ
- Симптом: в минус-словах кампании есть слово из глобальных правил, но НЕТ библиотечных минус-слов слепка (`{slepok}_minus_shared`). Скрин.
- Где: `create_set_minus.py::_apply_campaign_direct_minus` (campaign-уровень) и `::_get_or_create_minus_set` (library-набор); паковый читатель `_collect_pack_minus` (там же).
- Root-cause (2026-07-13, доказан): слепковый снапшот минусов `{slepok}_minus.txt` + `{slepok}_minus_shared.txt` (пак M3) применяется к кампании ТОЛЬКО в group-режиме (terehov/karavaev) — через групповые минусы `_build_tp2_adgroups g["minus"]` (`create_set_text_builders.py:64`). Для **campaign-режима** (pavlov/kryuchkova) и **shared_set-режима** (scherbakova) групповые минусы сняты (`apply_group_minus=False`), а campaign-уровневые аппликаторы брали ТОЛЬКО `_enabled_minus_words()` (глоб. вкладка), с комментарием «пак M3 как источник отключён — принцип только оттуда». Единственный читатель пака `_collect_pack_minus` был мёртвым кодом (0 реальных вызовов, только re-export shim в automation_runtime.py). Это противоречило: (а) group-режиму, который пак применяет; (б) комментарию `create_set_text_builders.py:63` «для campaign/shared_set минус — на кампании»; (в) редактируемости `_minus_shared` (сессия 2026-07-13). → слепковый минус не долетал до кампаний campaign/shared_set-режимов.
- Решение (2026-07-13, scoped, только `create_set_minus.py`, 2 функции): `_apply_campaign_direct_minus` и `_get_or_create_minus_set` теперь мержат `_collect_pack_minus(slepok, site_type, tp_code)` в `words` (дедуп case-insensitive, порядок: глоб. слова → паковые, кап `_minus_char_budget`). Гейт против двойного применения и переусердствования: пак мержится в campaign-inline ТОЛЬКО когда `_SLEPOK_MINUS_MODE != "group"` (group уже на группах) И `tp_code != "tp1"` (РСЯ — минуса режут охват без пользы, там же намеренно снят групповой минус). Пак недоступен (ssh M3) → try/except → деградация к глоб. словам (кампанию не валим).
- Верификация: py_compile OK; pyflakes — новых undefined нет (все варны — предсуществующие DI-инъекции). Offline-трейс (мок deps, ct с непустым `_minus_shared`): pavlov/tp2→глоб+пак, pavlov/tp1→только глоб, scherbakova/tp4→глоб+пак, terehov(group)/tp2→только глоб, unknown(default group)/tp2→только глоб, новый library-набор `_get_or_create_minus_set`→глоб+пак. Дедуп/порядок корректны.
- Остаточный гэп — ЗАКРЫТ (2026-07-13, доп. scoped-фикс cookie-пути): при ре-анализе гэп оказался НЕ в token-пути. Token-путь `_create_text_via_token` (feed_builders:410 attach Grid-набор + :437 `_apply_campaign_direct_minus`) УЖЕ кладёт слепковый минус INLINE через step 5 — т.е. фикс #9 его покрыл, даже при переиспользовании расшаренного набора. Реальный незакрытый путь — **cookie** `_create_text_via_cookie` (tp2/tp4): его `spec["minus_keywords"]` (Grid AddCampaigns, без баллов) нёс ТОЛЬКО глоб. слова, `_apply_campaign_direct_minus` там намеренно не вызывается (создал бы дубль), а переиспользуемый Grid-набор «Минуса общие» (`_grid_minus_pack_id`, feed_builders:189) слов слепка НЕ содержит → слепковый `_minus_shared` не долетал. Фикс: `_mk_words` = глоб. слова + `_collect_pack_minus` (зеркало гейта/дедупа/бюджета из `_apply_campaign_direct_minus`: только `mode!="group"` И `tp!="tp1"`, кап `_minus_char_budget` 20 000) кладётся INLINE в `spec.minusKeywords` per-кампания. Расшаренный аккаунтный набор НЕ мутируется (read-only reuse) → др. кампании, делящие набор, не затронуты. Deps `_collect_pack_minus`/`_minus_char_budget` добавлены в `_create_set_feed_builder_deps` (automation_runtime.py). Детектор `SLEPOK_MINUS_LIBRARY_MISSING` по-прежнему отложен (см. ниже).
- Статус: 🟡 оба фикса на Mac (Mutagen→LXC101 авто), py_compile+pyflakes+offline OK. Ждёт живого прогона на campaign/shared_set-слепке с непустым `_minus_shared` (pavlov/kryuchkova/scherbakova): в минус-словах поисковой кампании должны появиться слепковые фразы + глобальные — И на token-пути (баллы), И на cookie-пути (via_cookie, без баллов); расшаренный набор «Минуса общие» аккаунта не должен получить новых слов (проверить read-back набора до/после).
- НЕ помогло ранее: — (первая правка проброса пакового `_minus_shared` в campaign/shared_set аппликаторы). ⚠️ Реверс намеренного комментария «пак отключён — только оттуда»: разрешено в пользу поведения, консистентного с group-режимом, `create_set_text_builders.py:63` и редактируемым `_minus_shared`; при живом прогоне подтвердить, что охват поиска не просел сверх ожидаемого.
- 📌 Рекомендация (детектор, отложен — требует доп. инфры): `SLEPOK_MINUS_LIBRARY_MISSING` в `campaign_spec_audit.py`. Спецификация «`libraryMinusKeywordsIds` непуст» НЕКОРРЕКТНА при текущем фиксе — campaign-режим (pavlov/kryuchkova) кладёт слепковый минус INLINE (`NegativeKeywords`), а не как library → детектор дал бы ложные срабатывания. Корректный детектор должен читать паковый `_minus_shared` (extra `kp.gather`) и проверять покрытие inline ИЛИ содержимого library-набора (существующий `_audit_global_minus_campaign` содержимое library намеренно не резолвит — дорого). Это заметная инфра + риск ложных срабатываний → вынесено отдельной задачей, в scope #9-фикса не включено.

### GLOBAL_MINUS_ON_GROUP_DUPLICATED — глобальные минус-слова на группах И на кампании одновременно (2026-07-21, ИСПРАВЛЕНО)
- Симптом: «отзывы» стоит на 18/18 кампаний И на 522/522 групп porg-xjxpfxby. Группы получают глобальные слова дважды (на группе + кампания через `_apply_campaign_direct_minus`). Паковые ct-специфичные минусы (`data.get("minus")`) в group-режиме вообще не долетают до группы.
- Где: `create_set_text_builders.py:502` — `"minus": _enabled_minus_words()` клало глобальные минус-слова в каждую группу. `apply_group_minus=True` (дефолт "group" для неизвестного slепка, incl. kuderko) → все группы получали их. Кампания также получала их через `create_set_feed_builders.py:479` → `_apply_campaign_direct_minus` (:410 `words = list(_enabled_minus_words())`). Паковые ct-минусы (`{slepok}_minus.txt` + shared) НЕ попадали на группы (data["minus"] не читался).
- Решение (2026-07-21, direct_fixer): строка `:502` заменена на `"minus": data.get("minus") or []`. Теперь группа получает только ct-специфичные паковые минусы (или пустой список, если пака нет). Глобальные слова остаются ТОЛЬКО на кампании через существующий campaign-direct путь. Гейт `_SLEPOK_MINUS_MODE != "group"` в `_apply_campaign_direct_minus` при этом не меняется — пакмасать в campaign для group-режима было намеренно запрещено (коммент 2026-07-13). Для non-group режимов (pavlov/kryuchkova/scherbakova) `apply_group_minus=False` → `_gm=[]` → патч на них не влияет.
- Статус: 🟡 ждёт прогона (py_compile OK, логика проверена офлайн). Ожидаемый результат: глобальные слова — только на кампании (18/18), на группах — пусто ИЛИ пак-специфичные ct-минусы.
- НЕ помогло ранее: — (первый фикс).

#### 10. UI_BADGE_ONLY_AUTOTARGET — UI слепка показывает только автотаргетинг, а создались авто+КС (создание ВЕРНО)
- Симптом: реально создались корректно и автотаргет, и КС (разные РК) — это ПРАВИЛЬНО; но на странице «Структура слепков» бейдж = только «автотаргетинг». Проблема в ИНТЕРФЕЙСЕ, не в создании. Скрин.
- Где: UI — бейдж tp1-5 из `aon/aoff` кодера (index.html). = Класс 1 нашего аудита.
- Root-cause: бейдж парсит `aon/aoff`, который на поиске/РСЯ всегда `aon` и не отражает реальный КС. Метку строить из живого факта.
- Статус: 🟡 диагноз известен (Класс 1), фикс запланирован (бейдж из факта).



### CREDIT_IN_DEFAULT_TEXT_PRODUCT_TP3 — «кредит» в тексте по умолчанию tp3-товарной галереи (2026-07-13)
- Симптом: у товарного/каталожного объявления (ShoppingAd) в кампании tp3 «Товарная галерея» текст по умолчанию содержал «кредит» — нельзя для товарных/каталожных. Скрин прогона porg-asfbs7qe. (= триаж-ошибка #6.)
- Где: **`create_set_feed_builders.py::_create_tp3_single`** (~981-993, `set_default_text([shop], feed_id, data["default_text"])`). НЕ ai_agents.py: задача указывала ai_agents.py «вероятно», но проверенный путь оказался в feed-билдере.
- Root-cause (доказан трассировкой данных): `data` для tp3 берётся из общей `_tp5_account_data` (create_set_feed_builders.py:601). Там `default_text = next((t for t in slepok_content["texts"] if len(t)<=81), "")` (строка 636) — это КАМПАНИЙНЫЙ текст из `direct_slepok_content` (kind='campaign'), а он генерируется с кредитным углом (для авто-слепков `_credit_offer_ok_line` в create_content.py / `_text_ok` в ai_agents.py ТРЕБУЮТ кредит — это НАМЕРЕННО, дилерские сайты продают в кредит). Брат tp5 (`_create_tp5_single`:739) уже был исправлен R2-8 2026-07-10 на единый credit-free `SHOPPING_DEFAULT_TEXT` (create_set_assets.py:95, «ОДИН общий на все каталожные/товарные кампании»), а tp3 (`_create_tp3_single`:982) остался на `data["default_text"]` — переиспользовал кредитный текст текстового объявления как default text товарного. Асимметрия: R2-8 задекларировал «все каталожные/товарные», но tp3 пропустили.
- Решение (2026-07-13, `create_set_feed_builders.py::_create_tp3_single`): вместо `data["default_text"]` ставим `SHOPPING_DEFAULT_TEXT` (импорт `as _SDT3` с fail-safe фолбэком, как в tp5:712-714). set_default_text теперь вызывается всегда (как tp5:759), а не только при непустом slepok-тексте. Точечно, зеркалит принятый tp5-фикс; НЕ трогает кредитный угол текстовых объявлений (tp1/tp2 — там кредит намеренный для авто-слепков) и НЕ добавляет site_type-guard в `_credit_offer_ok_line` (это сломало бы легитимные дилерские тексты; «товарного/каталожного site_type» в системе нет — товарность определяется tp/типом объявления, а не site_type).
- Почему НЕ по образцу dmp-guard (как предполагала задача): dmp — это отдельный B2B site_type, где кредит не нужен ВЕЗДЕ. Здесь site_type авто-дилерский (Монобренд/Мультибренд/…), кредит нужен в ТЕКСТОВЫХ объявлениях; лишний он только в default text товарного объявления. Правильная точка — источник default text товарного (tp3), а не общий кредитный гейт контента.
- Верификация: py_compile create_set_feed_builders.py OK; pyflakes — только штатные DI-undefined (gf и др., globals().update(deps)), новых нет; `_SDT3` — локальный импорт, чист.
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), py_compile OK. Ждёт живого прогона: создать tp3-товарную галерею → ShoppingAd default text = «Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв.» (без «кредит»).
- НЕ помогло ранее: — (первая правка tp3 default text; tp5-аналог R2-8 2026-07-10 подтверждён как рабочий образец, не откат).

### HEADLINE_CHAR_BUDGET_UNDERUSE_TITLES — заголовки недобирают символьный бюджет (48-55 вместо ~56), LLM-добивка реализована (2026-07-13)
- Симптом: заголовки объявлений короче возможного — обычно 48-55 символов при лимите 56, много свободного места. Скрин прогона porg-asfbs7qe. (= триаж-ошибка #7.)
- Где: генерация заголовков — промпт `ai_agents.py::build_titles_messages` (~2482-2495) + density-upgrade pass в `create_content.py::run_gen_campaign_content` + dense-first sort перед `assemble_campaign`.
- Root-cause (найден): (1) промпт требовал «целься в 50-55 символов» — LLM (M3) не добирал стабильно; цель была заниженной. (2) В основном пути генерации НЕТ пост-добивщика: `assemble_campaign._pad` берёт ≥48 как есть. (3) Suffix-добивка сознательно отменена (campaign_spec_audit.py:1842 — суффикс к 54-симвному заголовку читается хуже). Решение Семёна: «ИИ сам добивает — не суффиксы».
- Решение (2026-07-13): три скоординированных изменения:
  1. **Новая константа** `TITLE_DENSE_MIN = 53` (ai_agents.py:777) — предпочтительный диапазон 53-56 симв, 48-52 = резерв приёмки, а не норма.
  2. **Промпт `build_titles_messages`** (ai_agents.py ~2482): цель изменена с «50-56» на «53-56». Добавлен раздел «ЭТАЛОН ДЛИНЫ» с парами BAD (48 симв) / НОРМА (53-56 симв) на конкретных примерах Haval-заголовков. Добавлено явное «48-52 = слабо, 53-56 = норма». dmp-ветка также обновлена (53-56).
  3. **Density-upgrade pass** в `create_content.py::run_gen_campaign_content` (перед count-retry, ~строка 377): после начальной приёмки собирает `_short_t = [t for t in good_t if len(t) < TITLE_DENSE_MIN]`; если список непуст — вызывает `build_density_upgrade_messages` + LLM M3 14B (1 вызов, bounded `_M3_CONTENT_IDLE_TIMEOUT`); LLM получает список коротких заголовков и инструкцию расширить каждый до 53-56 симв, добавив деталь (банки, «от», срок, взнос); каждый расширенный заголовок валидируется (len in [53,56], brand, credit/sale, digit, site_fit) и при успехе заменяет короткий in-place в `good_t`; seen_t/seen_t_norm обновляются. Exception-безопасен (нет валидного LLM-ответа → оставляем исходный короткий заголовок). **НЕ суффиксы** — LLM добавляет семантически осмысленную деталь.
  4. **Dense-first sort** `good_t.sort(key=...)` перед `assemble_campaign` (create_content.py ~662): если good_t > 1, ставит dense (≥53 симв) перед sparse (48-52 симв) — при избытке вариантов плотные идут в набор первыми.
- Brand-first: density-upgrade проверяет `_brand_re.search(exp)` перед заменой → бренд в начале сохраняется. Сорт не нарушает brand-first (только порядок в пуле, финальный набор — assemble/diversify).
- Лимит 56: `TITLE_DENSE_MIN <= len(exp) <= TITLE_MAX` — если LLM пере-расширил (>56), расширение отклоняется, исходный 48-симвный остаётся.
- Верификация: py_compile OK, pyflakes: 0 новых warnings (3 оставшихся — pre-existing escaped {{...}} в f-string Верни JSON в других функциях). Offline-симуляция: [48]-заголовок + LLM expansion [53] → заменяется; LLM [>56] → отклоняется; LLM [<53] → отклоняется; dense-first sort: [56],[53] до [48],[48]; brand-first: Haval первым во всех случаях.
- Статус: 🟡 реализовано на Mac (Mutagen→LXC101 авто), py_compile OK. Ждёт live-прогона: создать РК (не dmp, бренд задан) → заголовки должны быть ≥53 симв чаще; логи density_upgrade pass в stdout сервиса (Exception-caught, не фатален).
- НЕ помогло ранее: (1) промпт-инструкция «целься в 50-55» (2026-07-10) — LLM стабильно её недобирал. Текущий фикс = LLM-расширение + более высокая цель 53-56 + few-shot примеры.
- ⚠️ Суффиксную добивку `extend_title_to_max` НЕ включали и не возвращали — решение «суффиксами не добиваем» сохранено, LLM работает семантически.

### IMAGES_REPAIR_CONTENT_GAP_FALSE_FAIL — images_repair валил весь шаг из-за контент-гэпа + вводящее в заблуждение сообщение (2026-07-13)
- Симптом: авто-добивка (job 0d2708907d6d, scherbakova/Мультибренд) помечала images_repair кампании `ok=False` с текстом «не удалось загрузить картинки ни для одного ct из кп», хотя механизм картинок ИСПРАВЕН (в create-прогоне залилось 79/81 и 253/258). Провал только на объявлениях ct, у которых нет креативов НИ В ОДНОМ источнике: ct0067(Dongfeng), ct0195(MG), ct0041(Changan UNI-S/CS55Plus), ct0052(Chery Tiggo 7L), ct0066(Dongfeng 580), ct0070(Dongfeng DFSK 500), ct0072(Dongfeng DFSK ix7).
- Где: `create_set_repairing.py::_campaign_images_repair` (~302-375). Резолвер `_creative_images_for_ct` (automation_runtime.py:2499) для gap-ct возвращает [] → hashes пуст → items пуст → выброс общей ошибки «ни для одного ct» → весь images_repair кампании ok=False.
- Root-cause: код не отличал КОНТЕНТ-ГЭП (резолвер вернул [] — нет путей к файлам, грузить нечего) от UPLOAD-FAIL (пути были, но upload/хеши не получились). Оба сливались в один hard-fail, а сообщение «ни для одного ct» ложно указывало на поломку загрузчика.
- Решение (2026-07-13, `create_set_repairing.py`): в цикле по ct — если `paths==[]` → `content_gap_cts.append(ct); continue` (не считаем ошибкой, не трогаем reset/upload); если пути были, но `hashes` пуст → `upload_fail_cts.append(ct)`. При `not items`: `upload_fail_cts` непуст → ok=False «upload-fail: не удалось загрузить картинки для ct [...]»; иначе (чистый контент-гэп) → **ok=True**, `ads_updated=0`, `skipped_content_gap=True`, note «контент-гэп: нет креативов для ct [...] (нужны картинки в Manual/<ct> или паке слепка)». Успешный путь: `ok = updated>0 and not upload_fail_cts`. В результат всегда добавлены поля `content_gap_cts` / `upload_fail_cts`, чтобы контент-гэп не считался hard-fail'ом добивки.
- Верификация: py_compile create_set_repairing.py OK; ветки прочитаны (content-gap→ok=True со списком; upload-fail→ok=False; смешанное с частичным успехом→ok=False+error, но content_gap_cts не в счёт).
- Статус: ✅ фикс 2026-07-13 (разделение content_gap/upload_fail) + доп. фикс 2026-07-22 (Баг A ниже).
- НЕ помогло ранее: — (первая правка разделения контент-гэп/upload-fail в images_repair). Примечание «вечного цикла нет» из журнала 2026-07-13 оказалось НЕВЕРНЫМ: `skipped_content_gap=True` с `ok=True` попадало в `executed` → anti-pingpong не срабатывал → 2 итерации без прогресса → partial → reschedule. Закрыт фиксом Баг A 2026-07-22 (IMAGE_NO_POOL, `repair_media.py`+`queue_server.py`).

### ADPRICE_REPAIR_STUB_SILENT_FAIL — adprice_repair это заглушка, всегда падала молча в цикле добивки (2026-07-13)
- Симптом: для tp1 Фиды-Автотаргетинг NO_ADPRICE_LIVE планировался как исполнимое действие, а исполнитель звал стаб → `{"ok": False, "note": "...не реализован..."}` БЕЗ ключа `error` → `_run_per_campaign_repair` синтезировал `error="ok=False"`, per-campaign error=None (молчит). Провал гарантирован каждый прогон.
- Где: `create_set_repairing.py::_campaign_adprice_repair` (~378-392, стаб); планирование — `grid_content_verifier.py:159-162` (NO_ADPRICE_LIVE + repair-candidate) → `repair_planner.py:325/462` (action="adprice_repair") → `repair_gate.executable_adprice_repairs` → `repair_auto.execute_all_in_place:315-325`.
- Root-cause: функция — нереализованный стаб (нужен rebuild price_map из offer_prices + ad→brand маппинг), но detect/plan/gate считали adprice_repair executable_now → добивка гарантированно исполняла его и получала безмолвный ok=False.
- Решение (2026-07-13, минимальный вариант, 2 файла): (1) `repair_gate.executable_adprice_repairs` теперь возвращает `[], [], matched` — НИ одного executable id, все adprice_repair-действия → unsupported → `execute_all_in_place` пропускает блок (adprice_ids пуст), `summarize_repair_gate.executable_now` больше не включает adprice → цикл не гоняет reschedule на гарантированно-провальном ремонте. (2) стаб `_campaign_adprice_repair` дополнен ключом `error="adprice_repair не реализован (стаб; ...)"` — на случай прямого вызова провал не безмолвный. Полный adprice НЕ реализован (отдельная задача). NO_ADPRICE_LIVE остаётся видимым как known non-fixable гэп (unsupported).
- Верификация: py_compile create_set_repairing.py + repair_gate.py OK; трасса подтверждена — оба executor-пути (repair_auto:98/315) и summarize (repair_gate:429) зовут `executable_adprice_repairs`, централизованная правка покрывает все.
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), py_compile OK. Ждёт живого прогона добивки на tp1 Фиды-Автотаргетинг: adprice_repair не должен исполняться/падать, NO_ADPRICE_LIVE помечен неисполнимым.
- НЕ помогло ранее: — (первая правка; стаб был замкнут в pipeline detect→plan→gate→executor и всегда падал). Вернуть adprice в executable ТОЛЬКО после реализации стаба (price_map из offer_prices).

### TP7_FEED_FANOUT_IGNORES_EXPLICIT_FEED — tp7-товарка размножалась по ВСЕМ фидам, игнорируя явно заданный фид позиции (2026-07-12)
- Симптом: tp7 (Товарка) создавала кампанию на КАЖДЫЙ разрешённый URL-фид аккаунта (fan-out), даже когда позиция слепка подразумевала конкретный фид → могли появляться кампании на нерелевантных фидах.
- Где: `create_set_plan.py::_emit_struct` (feed_list, ~775). `feed_list = [(id,name,url) for f in feeds]` для product — безусловный fan-out по всем `feeds` (уже отфильтрованным allow-list'ом, но без учёта фида позиции). Структура позиции (`_slepok_struct_groups`, create_set_context.py:164) фид вообще не пробрасывала.
- Root-cause: `_slepok_struct_groups` не извлекала фид позиции, а `_emit_struct` всегда фанил по `feeds`. В текущих данных slepki_structure.json фид на позициях НЕ задан ни у кого (feed/role keys = 0), поэтому fan-out был единственным путём.
- Решение (2026-07-12, точечно, 3 файла):
  - `create_set_context.py::_slepok_struct_groups` — в возвращаемый item добавлены `feed_role`/`feed_id`/`feed_key` (item→group приоритет; пусто по умолчанию).
  - `create_set_plan.py` — хелперы `_feed_role_of` (catalog/landing по `_CATALOG_FEED_KEYS`) и `_explicit_feed_subset(g, feeds)`: явный фид (id→ключ/имя→роль) → подмножество; не задан → `None` (fan-out); задан, но нет среди разрешённых → `[]` (позиция пропускается + warning, НЕ на чужом фиде). `_emit_struct`: product-ветка использует subset; fan-out по умолчанию + ОДИН warning на `_emit_struct` («без явного feed — fan-out по N фидам»).
  - `automation_runtime.py::_create_set_plan_deps` — инъекция `_feed_key` и `_CATALOG_FEED_KEYS`.
- Обратная совместимость: слепки без явного feed → `_explicit_feed_subset` возвращает `None` → прежний fan-out (проверено на pavlov/tp7: feed_role='' feed_id=None → None). Меняется только добавленная строка лога.
- Верифицировано (офлайн): py_compile 3 файлов OK; таблица веток `_explicit_feed_subset` (no-spec→None, id/key/role→match, absent→[]) — все корректны; `_slepok_struct_groups` на pavlov/Мультибренд/tp7 отдаёт пустые feed-поля (any explicit=False). Live-прогон не запускался.
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), py_compile OK, офлайн-логика верна. Ждёт: (1) живого tp7-прогона с fan-out (должен появиться warning, поведение прежнее); (2) при появлении явного feed_role/feed_id в структуре — товарка ровно на нём.
- НЕ помогло ранее: — (первая правка проброса фида позиции в tp7). Смежно: allow-list фильтр `feeds` (create_set_plan.py:314-325) уже отсекал неразрешённые фиды, но НЕ давал таргетинга на конкретный фид позиции — это ортогонально и оставлено.
- Смежная диагностика (Task 2 — cpc+cpa дублирование, kuderko): механизм НАЙДЕН, но это ОСОЗНАННАЯ фича, НЕ баг → код НЕ трогал. `pays = ["tcpa"] if no_cpa else ["tcpa","cpa"]` (create_set_plan.py:458, `no_cpa=bool(body.get("n"))`) — по умолчанию (галочка «под стиль сайта»/n снята) каждая позиция даёт пару cpc+cpa; снять дубль = поставить «n». Это deliberate-решение Семёна 2026-07-12 (fix #4, коммент :749-750 «МК — тоже по галочке, единый механизм pays. Было: не-авто МК всегда cpa, 1 РК»). Per-position pricing в коде структуры (`tp7_cpc_site` vs `tp7_cpa_site`) при этом `_emit_struct` игнорирует — pays определяется галочкой аккаунта, а не токеном cpc/cpa в `c`. kuderko (реально наблюдаемый аккаунт) tp6/tp7 НЕ содержит вовсе — у него tp1/tp2/tp5, все позиции `cpc`, дублирование — тот же checkbox-механизм. Правка = откат deliberate-фичи → требует явного решения Семёна (должен ли per-position `c`-pricing перекрывать галочку?), слепой фикс запрещён.

### UAC_LIST_CAMPAIGNS_405 — GET /web-api/uac/campaigns → 405 Method Not Allowed (2026-07-12)
- Симптом: `UacClient.list_campaigns()` бил `GET /web-api/uac/campaigns`, Яндекс отвечал 405 (метод не поддерживается на этом эндпоинте) → исключение. Единственный вызов — `routes_content_editor._load_account:683` (`uac_detail_client.client.list_campaigns()`), где 405 гасился `except` и добор UAC-кампаний шёл через Grid-фолбэк `_grid_tp67_campaigns` (т.е. рабочий путь уже был, но снаружи метода — сам метод всегда падал).
- Где: `campaign.py::UacClient.list_campaigns` (~1468); рабочий Grid-путь — `routes_content_editor.py::_grid_tp67_campaigns` (~361), фактически исполнявшийся фолбэк — `routes_content_editor.py:700`.
- Root-cause: приватный UAC-эндпоинт `/web-api/uac/campaigns` не отдаёт список методом GET (405). UAC-кампании (Мастер tp6 / Товарка tp7) невидимы в v5 и читаются только через Grid GraphQL `client.campaigns` (фильтр tp6_/tp7_ по имени, без архивных) — ровно то, что делает `_grid_tp67_campaigns`.
- Решение (2026-07-12, `campaign.py`): `list_campaigns` переведён на делегирование к `_grid_tp67_campaigns(self.ulogin)` (lazy import — иначе цикл routes_content_editor→uac_read→campaign), с сохранением фильтра `status`. Выбран вариант «вызвать существующую функцию» (0 дублирования GraphQL/пагинации) вместо повтора запроса в campaign.py. Внешний Grid-фолбэк в routes_content_editor остаётся как безвредный ретрай.
- Верификация: py_compile OK; единственный caller (`routes_content_editor:683`) читает id/name из dict — оба поля присутствуют в выдаче `_grid_tp67_campaigns` (id, name, typename, status); `self.ulogin` == login (build_client(ulogin)→UacClient(cookie, ulogin)).
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), ждёт живого прогона content-editor `_load_account` на аккаунте с tp6/tp7-черновиками (список UAC приходит без 405).
- НЕ помогло ранее: —

### AUDIENCE_GOALS_SILENT_TRUNCATE_100 — >100 аудиторных сегментов молча резались в payload UAC (2026-07-12)
- Симптом: при 100+ id аудиторий в спеке `build_payload` брал первые 100 без единого следа — «лишние» сегменты терялись тихо, кампания создавалась с усечённым таргетингом.
- Где: `campaign.py::UacClient.build_payload` (~1591), `ca_retargeting_condition.condition_rules[0].goals`.
- Root-cause: `[:100]` — это РЕАЛЬНЫЙ лимит API Яндекса (не более 100 goals в одном condition_rules, иначе `INVALID_COLLECTION_SIZE`), подтверждён комментарием рядом (`_slepok_audiences_for` объединяет все категории → может дать 200+ id). Проблема была не в самом капе, а в его молчаливости.
- Решение (2026-07-12, `campaign.py`): кап оставлен (лимит API реален), но при `len(_all_goals) > 100` печатается WARNING в stderr (стиль как `create` line ~1996) с числом сегментов, сколько отброшено и display_name — обрезка перестала быть тихой. Поведение payload не изменилось (те же первые 100).
- Верификация: py_compile OK; `sys` и `DEFAULT_DISPLAY_NAME` в области видимости (импорт line 62, константа используется в этом же методе line ~1525).
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), ждёт прогона со спеком >100 аудиторий — в журнале воркера должен появиться WARNING о потере N сегментов.
- НЕ помогло ранее: —

### SINGLE_FEED_TP5_TP3_WRONG_FEED — при «по одному фиду» tp5/tp3 создавались на ЧУЖОМ фиде (credit-page), вразрез с планом (2026-07-12)
- Симптом: галочка «по одному фиду» (single_feed) на аккаунте БЕЗ `/yandex.xml` — товарные галереи tp5 (Поиск+Динамика+ТГ) и tp3 (ТГ) создавались на первом попавшемся разрешённом фиде (на porg-asfbs7qe это `credit-page-01-a.xml` — лендинг/оффер-фид, не товарный), тогда как plan/превью и tp7 резолвили `/yandex.xml` или канонический фолбэк. Выглядело как «галочка не работает».
- Где: tp5/tp3 (token/API-путь), `create_set_feed_builders.py::_create_tp5_campaign` (~858-863) и `_create_tp3_campaign` (~987-992); резолв фида — `_tp5_account_data.feeds` → `prefer_single_feed_variants`.
- Root-cause: tp5/tp3 items эмитятся структурой слепка НЕЗАВИСИМО от plan-`feeds` (create_set_plan.py:561-583/661-702), т.е. само-резолвят фид. `prefer_single_feed_variants(data["feeds"])` при отсутствии `/yandex.xml` тихо возвращает `variants[:1]` = ПЕРВЫЙ разрешённый фид. Хотя url `/yandex.xml` уже лежал в кортеже (`UrlFeed.Url`, line 615) и Grid тоже отдаёт url — фида `/yandex.xml` в аккаунте просто НЕТ (51 фид, все `yandex-<brand>.xml`/`yandex-catalog-*.xml`), поэтому prefer брал первый. tp1/plan вместо этого идут через `_first_url_feed(strict=True)` (create_set_plan.py:350): strict-мисс /yandex.xml → канонический фолбэк `yandex-catalog-model-design-custom-name.xml` при подтверждённом `single_feed_fallback`, иначе feeds=[] (товарные не создаются).
- Живой факт (v5 feeds.get + UrlFeedFieldNames Url, porg-asfbs7qe/victoryagency14, 2026-07-12): `/yandex.xml` в v5 ОТСУТСТВУЕТ; OLD prefer_single_feed_variants → 3505256 credit-page-01-a (ЧУЖОЙ); NEW resolution → strict/yandex.xml=0, fallback=3505268 yandex-catalog-model-design-custom-name.xml; single_feed_fallback=False→[] (skip, как plan/tp7), =True→[3505268]. Вариант (a) «мержить Grid всегда» и (b) «читать UrlFeed.Url в allow-фильтре» — оба НЕ помогли бы: /yandex.xml физически нет ни в v5, ни в Grid, а url уже был в кортеже.
- Решение (2026-07-12, `create_set_feed_builders.py`): новый helper `_resolve_single_feed_variants(data, token, login, agency, job)` — резолвит целевой фид как plan: `_first_url_feed(strict=True)` для /yandex.xml → фолбэк `_first_url_feed(strict=True, url_key=FALLBACK_SINGLE_FEED_KEY)` ТОЛЬКО при `job.body.single_feed_fallback` → выбор кортежа из data["feeds"] по id; не резолвится → feeds=[] (tp5/tp3 явно пропускается, а не создаётся на чужом фиде). tp5/tp3 зовут helper вместо `prefer_single_feed_variants`. `_first_url_feed` добавлен в `automation_runtime._create_set_feed_builder_deps()`.
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), py_compile OK, логика верифицирована живыми данными (offline-симуляция резолва). Ждёт живого прогона: single_feed на аккаунте с `/yandex.xml` → tp5/tp3 фан-аут ровно на него; без /yandex.xml — фолбэк-фид при подтверждении, иначе пропуск (не credit-page).
- Верификация direct_verifier (2026-07-12): ✅ код подтверждён — helper `_resolve_single_feed_variants` (create_set_feed_builders.py:842-858) зовётся в tp5 (881-885) и tp3 (1013-1017) вместо `prefer_single_feed_variants`; `_first_url_feed` добавлен в DI `automation_runtime._create_set_feed_builder_deps()` (2828); non-single_feed путь (fan-out по всем фидам) не тронут; отклонённые (a)/(b) записаны корректно. Остаётся 🟡 только на live happy-path (аккаунт с реальным `/yandex.xml`).
- НЕ помогло ранее: — (первая правка резолва single_feed в tp5/tp3). Вариант (a)/(b) из диагностики отклонены живым фактом (см. выше), не пробовать.
- FOLLOW-UP (2026-07-13, feed_alert НЕ покрывал tp5): предыдущий фикс (2026-07-12) сделал tp5/tp3 при отсутствии /yandex.xml и без подтверждённого фолбэка ПРОПУСКАЮТСЯ (feeds=[]) — но при наборе, где есть tp5 (search_gallery), а tp7/tp3 нет, поп-ап feed_alert НЕ всплывал, и tp5 терялся молча. Причина: `_fal_needed = len(feeds)==0 and (want_product or want_tp3)` (create_set_plan.py:840) покрывал только tp7/tp3, флага для tp5 не было (в блоке tp5 create_set_plan.py:695-734 want_* не ставился, в отличие от want_tp3 на :607). Живой прогон job 3f689a9ccec2, scherbakova/Мультибренд, single_feed=true. Решение: введён `want_tp5_gallery` (init :493, ставится в блоке tp5 после подтверждения tp5_items :703), добавлен в `_fal_needed` (:840→ `or want_tp5_gallery`) и в `will_skip_types` (:849 → ключ `"tp5"`). Фронт: FEED_SKIP_LABELS/FEED_SKIP_PLAN_TYPES (templates/direct/index.html:3048-3049) получили ключ `tp5` → label «Товарная галерея (tp5)», plan-type `search_gallery` (совпадает с plan[].type в create_set_plan.py:722/730). Путь фолбэка для tp5 идентичен tp3: `_resolve_single_feed_variants` (create_set_feed_builders.py:842-858) без ветвления по tp_code, оба через `_first_url_feed(strict, url_key=FALLBACK_SINGLE_FEED_KEY)` при `single_feed_fallback`. Статус: 🟡 ждёт живого прогона — «single_feed on + нет /yandex.xml + есть tp5» должен показать поп-ап с кнопкой «Продолжить с другим фидом», при подтверждении tp5 строится на фолбэк-фиде. py_compile OK.

### SLEPOK_CORPUS_SITETYPE_BLEED — бренды одного site_type протекали в секции другого при пересборке слепков (2026-07-12)
- Симптом: латентный (видимого вреда не было — «для scherbakova случайно совпало»). При автоперестройке структуры слепков бренды «С пробегом» могли попасть в «Мультибренд» и наоборот.
- Где: **`scripts/build_slepok_structure.py::scan_corpus`** (~62–107). Собирала ct4-коды (adgroups) и tp-коды (campaigns) в ОДИН плоский set на всего директолога, без разбивки по site_type. `rebuild_directologist_section` применял этот общий set ко ВСЕМ секциям → фильтр `filter_items` пропускал бренд, если ct4 встречался в корпусе хоть где-то (в т.ч. в логине чужого site_type).
- Root-cause: site_type в корпусе задаётся на уровне login (`_logins.json` → поле `type`), но scan_corpus его игнорировал. Пример: terehov ct0150 присутствует только в логинах «С пробегом», но в суперсете «Мультибренд» флаг-фильтр его сохранял (bleed). Аналогично tp-gate: tp3 (С пробегом) считался «используемым» и в «Мультибренд».
- Решение (точечный патч, 2026-07-12): scan_corpus читает `_logins.json` (login→site_type) и возвращает `used_tps_by_st`/`corpus_ct4_by_st` = `{site_type: set}`. Логины без записи → бакет `_UNKNOWN_ST=""`, подмешивается во все site_type через `_st_sets` (в реальном корпусе такие логины бренд-ct4 не содержат — 0). `rebuild_directologist_section` для каждой секции берёт набор ИМЕННО её site_type. `build_staging` считает плоские агрегаты только для логов/отчёта; фильтрация — по site_type. filter_items не менялся.
- Статус: 🟡 ждёт прогона. Офлайн-факт (read-only, без записи в живые файлы): terehov «Мультибренд» ДО содержал С-пробегом-only `ct0150`; ПОСЛЕ — убран, остаётся только `ct0000` (общий фид-код, всегда keep by design); секция «С пробегом» сохраняет все 14 своих ct4; tp3 не течёт в «Мультибренд». py_compile OK. Реальный прогон `build_staging` — решение Семёна.
- Верификация direct_verifier (2026-07-12): ✅ код подтверждён — `scan_corpus` возвращает `(used_tps_by_st, corpus_ct4_by_st)` (dict по site_type), helper `_st_sets`, `rebuild_directologist_section` берёт набор своей секции, `filter_items` не тронут; при отсутствии `_logins.json` фолбэк через `_UNKNOWN_ST` = прежнее flat-поведение → регрессий нет. Остаётся 🟡: нужен реальный `build_staging --output` в staging-файл (НЕ поверх живого `slepki_structure.json`), сверка `collisions_before/after` — решение Семёна.
- Остаток (НЕ в этом фиксе): per-tp cross-join campaigns↔adgroups (ассоциация ct4 с конкретным tp через CampaignId) не реализован — это глубже и не требуется для устранения site_type-bleed.
- НЕ помогло ранее: —

### KEYWORD_REPAIR_PARTIAL_REPORTED_ZERO — беспаковая группа роняла ok/executed всей докрутки в «0 действий» (2026-07-12)
- Симптом: докрутка ключей обрабатывает N групп (кейс: 44), 43 группы ключи получили успешно, но виджет показал «добилось 0 действий» → сработал anti-ping-pong break → reschedule всей пачки вхолостую. Врёт про прогресс.
- Где: **`repair_executor.py:execute_keywords_repair`** — ветка `need_kw and not writable_kw and not need_at` (беспаковая КС-группа) клала группу в `failed`; на выходе `ok = not failed = False`, status 207. Далее `repair_auto.py:execute_all_in_place:286` считает `executed` только при `200<=status<300 AND out.get("ok")` → ok=False → executed=0. `queue_server.py:1022` `if not res.get("executed"): break` (anti-ping-pong) + `remaining>0` (live NO_KEYWORDS_LIVE у беспаковой) → reschedule.
- Root-cause: одна беспаковая группа (пак пуст/недоступен — нечинимый СЕЙЧАС остаток, аналог video_no_pool, НЕ провал докрутки) попадала в тот же bucket `failed`, что и настоящие провалы → отравляла агрегатный `ok` → 43 реально применённые группы репортились как 0.
- Решение (2026-07-12, `repair_executor.py`): беспаковые КС-группы кладём в ОТДЕЛЬНЫЙ bucket `unfixable` (не `failed`). `ok = (not failed) and (bool(applied) or not unfixable)` — партиал-успех (applied>0) даёт ok=True/200 → executed отражает реальный прогресс; если применить не удалось НИЧЕГО (только беспаковые) → ok=False/502 (честно «0 добили», НЕ маскируем под «идемпотентно»). Идемпотент-ветка `not write_items and not failed` дополнена `and not unfixable`. В ответ добавлены `unfixable_no_pack`/`unfixable_groups` → audit↔repair не расходятся. Изменение затрагивает ТОЛЬКО mixed-кейс (applied>0 + беспаковые + без реальных провалов): ok False→True; все прочие пути (all-unfixable, реальные провалы, штатный успех) байт-в-байт прежние.
- Статус: 🟡 код на Mac (Mutagen→LXC101 авто), py_compile OK. Ждёт живого прогона докрутки с частично-беспаковой пачкой.
- Верификация direct_verifier (2026-07-12): ✅ код подтверждён — bucket `unfixable` изолирует беспаковые КС-группы от `failed`; `ok = (not failed) and (bool(applied) or not unfixable)` даёт mixed=ok/200 и all-unfixable=ok False/502 (инвариант KEYWORD_REPAIR_NO_PACK_SILENTLY_OK сохранён); при `unfixable=[]` формула сводится к старому `not failed` → регрессий нет; NameError-риска нет (`applied` определён до `ok`). Остаётся 🟡: live-докрутка на частично-беспаковой пачке.
- НЕ помогло ранее: KEYWORD_REPAIR_NO_PACK_SILENTLY_OK (2026-07-10) — правильно завёл беспаковую-КС в `failed` (чтобы не выдавать «всё ок»), но переусердствовал: одна беспаковая группа стала ронять весь батч в «0 действий». Текущий фикс сохраняет тот инвариант ТОЛЬКО для all-unfixable (ok=False), а mixed теперь честно партиал.

### PREFLIGHT_CROSS_SITETYPE_DUP_BLINDSPOT — preflight не ловил дубли структуры между site_type одного слепка (2026-07-12)
- Симптом: НЕ ошибка создания РК, а слепой угол детектора. `slepki_preflight.py` находил байт-идентичные секции структуры ТОЛЬКО между разными директологами (фамилиями), но не между site_type ОДНОГО директолога. По корпусу незамечено 25 групп / 48 лишних копий (kryuchkova и salamahin — все 4 site_type идентичны; chepelev — Монобренд=Мультибренд).
- Где: `scripts/slepki_preflight.py::check` — фильтр `len({x[0] for x in v}) > 1` (уникальные `key`=директолог внутри bucket `fp[frozenset(items)]`) ловит только межфамильные коллизии; одинаковые секции разных site_type одного директолога дают `len({key})==1` → не флагались.
- Root-cause: `_sig` группирует секции по `(site_type, tp)`, но детектор считал уникальность по `key` (директолог), а не по `site_type` внутри одного директолога.
- Решение (2026-07-12, только детектор/отчёт): в `check()` добавлен блок 1b (для каждого директолога секции `_sig(e)` группируются по `frozenset(items)`; bucket'ы, где один frozenset встречается под >1 site_type → `same_slepok_cross_site`) + НЕблокирующий ⚠-отчёт (число групп / лишних копий + построчная раскладка `key: site/tp`). `preflight_dict` (блокирующий путь редактора слепков) НАМЕРЕННО не тронут — kryuchkova/salamahin идентичны намеренно (решение Семёна), блокировка сломала бы редактор. Сами структуры слепков НЕ менялись.
- Статус: ✅ детектор проверен read-only прогоном (`python3 scripts/slepki_preflight.py`): 25 групп / 48 лишних копий — совпало с независимым пересчётом, EXIT=0 (non-blocking); существующая межфамильная проверка не задета (cross-slepok коллизий 0, как раньше). Код независимо подтверждён direct_verifier (правка хирургическая: блок 1b строки 188-200 + отчёт 266-274, `ok=False` НЕ выставляется, регрессий в empty_tp/bad_gc/duplicate_items/source_manifest нет).
- Остаток (НЕ код): намеренность идентичных структур (kryuchkova/salamahin/chepelev) — решение Семёна отдельно; детектор только сигнализирует, деплой не блокирует.
- НЕ помогло ранее: —

### SCHERBAKOVA_SALE_TITLE_CUT — голос Щербаковой резался кредитным фильтром заголовков (2026-07-12)
- Симптом: для брендовых кампаний фирменные sale-заголовки Щербаковой («Распродаём стоянку. Скидка 45%», «Распродаём перед завозом», «Нулевой утильсбор. Господдержка …») систематически отбрасывались из LLM-контента.
- Где: `create_content.py:_title_ok` (кредитный гейт заголовков в `run_gen_campaign_content`).
- Root-cause: `if not _DIRECT_CREDIT_RE.search(t): return False` жёстко требовал кредитное слово в КАЖДОМ заголовке. Sale-фразы «складских» слепков (распрод/склад/стоянк/завоз/утильсбор) кредитного слова не содержат → резались. В `ai_agents.py:assemble_campaign._title_ok` (стр. 2042-2047) верная OR-альтернатива `title_sale_ok_re` уже была; в `create_content.py` — нет (рассинхрон двух фильтров).
- Решение (2026-07-12, `create_content.py`): добавлен `_TITLE_SALE_OK_RE = re.compile(r"(?i)(распрод|склад|стоянк|завоз|утильсбор)")` (образец — `ai_agents.py:2021`, +утильсбор как фраза Щербаковой); гейт заменён на `if not (_DIRECT_CREDIT_RE.search(t) or _TITLE_SALE_OK_RE.search(t)): return False`. `_text_ok` НЕ тронут (текст держим в кредитной рамке, как в ai_agents).
- Верифицировано (офлайн, но НЕ на боевом гейте): 5 кейсов на `_title_ok` PASS/FAIL корректно. py_compile OK. ОДНАКО тест гонял МЁРТВУЮ функцию — см. Статус.
- Провал первой правки (2026-07-12): фикс попал в МЁРТВЫЙ КОД. `_title_ok` в модуле НИГДЕ не вызывается (0 callers, подтверждено grep) → `_TITLE_SALE_OK_RE` и OR в нём на runtime НЕ влияют. Реальный гейт LLM-заголовков — `_accept_title` (вызовы: 4 места в `run_gen_campaign_content`), где credit-only фильтр без OR резал sale-заголовки Щербаковой (независимо подтверждено direct_verifier).
- Решение (2026-07-13, `create_content.py:_accept_title`, теперь строка ~265): credit-only гейт заменён на `if not (_DIRECT_CREDIT_RE.search(t or "") or _TITLE_SALE_OK_RE.search(t or "")):` — в реальном гейте. `_TITLE_SALE_OK_RE` НЕ пришлось поднимать: он уже определён в области функции `run_gen_campaign_content` (строка ~64, рядом с `_DIRECT_CREDIT_RE`), общей для `_title_ok` и `_accept_title` через closure — вопреки формулировке next-step, он НЕ был вложен в `_title_ok`. Мёртвый `_title_ok` не удалён (сиблинги `_text_ok`/`_ok` — тот же dead-кластер, удаление = unrelated cleanup вне точечного фикса), но помечен явным комментом «МЁРТВЫЙ КОД, реальный гейт — _accept_title» (строка ~112), чтобы больше не вводил в заблуждение. py_compile OK.
- Статус: 🟡 ждёт прогона — фикс теперь в РЕАЛЬНОМ гейте `_accept_title`. Нужна live-проверка: прогнать брендовую кампанию «складского» слепка (Щербакова) и убедиться, что sale-заголовки («Распродаём стоянку. Скидка 45%», «Нулевой утильсбор…») проходят в LLM-контент, а не режутся в `missing_credit_angle`.
- НЕ помогло ранее: OR-альтернатива в `create_content.py:_title_ok` (2026-07-12) — правка в dead code (`_title_ok` не вызывается, 0 callers). НЕ повторять правки в `_title_ok`/`_text_ok`/`_ok` — это мёртвый кластер; реальные гейты LLM-output — `_accept_title`/`_accept_text`.

### DMP_SITELINKS_AUTO_BLEED — B2B-сайтлинки dmp заменялись авто-ассетами аккаунта (2026-07-12)
- Симптом: у слепка dmp быстрые ссылки в кабинете = авто-сферы («Первый взнос 0₽», «Оценка авто в трейд-ин», «КАСКО на 1 год», «Тест-драйв», «Авто в наличии») — вместо B2B-контента; porg-lrfjzcxo.
- Где: tp2 поиск (`create_set_feed_builders.py:77` — `_common_sitelinks_fast`) и tp6 МК (`blueprint.py:6707` — `_slepok_campaign_content`). Оба пути фильтруют `isinstance(s, dict) and s.get("title")` → строки из БД отсеиваются → возвращают [] → аккаунтные авто-ассеты.
- Root-cause: `public.direct_slepok_content` (slepok='dmp', site_type='dmp', kind='campaign') хранит sitelinks как список строк: `["Автоматическая интеграция","От 200 ₽ за контакт",...]`. Legacy-засев через M3 без dmp-контекста сохранял строки. `_seed_slepok_content` итерировал только `SITE_TYPE_PROFILE.keys()` (авто-типы), НЕ охватывал `site_type="dmp"` → запись не обновлялась. Корректный B2B-пул (`AGENT_ADS["dmp"]["sitelinks"]` + `sitelink_bank_for("dmp")`) существовал в `ai_agents.py`, но до БД не доходил.
- Решение (2026-07-12, `ai_content.py`):
  1. `_slepok_content_get` (строки 352–385): нормализация строк → `{"title": s, "description": ""}` перед возвратом — немедленно чинит path 1 (`_common_sitelinks_fast` через `_slepok_content_get`) на текущих данных БД.
  2. `_seed_slepok_content` (строки ~501–515): доп. цикл по агентам с `site_fit` вне SITE_TYPE_PROFILE (dmp → extra_sts=["dmp"]). При `--all` вызывает `assemble_campaign([], [], [], dmp_agent, site_type="dmp")` → записывает 8 B2B dict-сайтлинков в (dmp, dmp, campaign) — чинит path 2 (blueprint.py прямой запрос в БД) после запуска seed.
- Требует запуска на LXC101: `python3 -m direct.seed_slepok_content --all` (чтобы перезаписать стрингов-запись ключом `--all`; без него `only_missing=True` пропускает существующую). После: оба пути возвращают 8 B2B dict-сайтлинков.
- Верифицировано (офлайн, факт): `assemble_campaign(dmp, site_type=dmp)` → 8 B2B dict-ссылок (`"Получите демо-доступ сегодня"`, `"Горячие контакты за 24 часа"`, …), 0 авто-слов; трассировка обоих путей с корректной БД → picked=8 B2B, [] не возникает. py_compile+pyflakes OK.
- Статус: 🟡 код на Mac (Mutagen→LXC101 авто). Нужно: (1) рестарт direct-create/worker, (2) `python3 -m direct.seed_slepok_content --all` на LXC101.
- Граблей НЕТ (старый): авто-слепки затронуты НЕ будут — их `site_fit` целиком в `SITE_TYPE_PROFILE.keys()` → `extra_sts=[]` → цикл их игнорит.

### TP6_MASTER_REQUIRES_FEED — ложный диалог «Мастер кампаний не создать без фида» (2026-07-11)
- Симптом: набор с tp6 (Мастер кампаний) без URL-фида на аккаунте → диалог «⚠️ Фид /yandex.xml не найден. Мастер кампаний (tp6) не смогут быть созданы (нет URL-фида). Через 5 минут будут автоматически запущены кампании без них.» + кнопка «Создать без них (Мастер кампаний tp6)». (Кейс: слепок dmp на porg-lrfjzcxo — у dmp только tp2+tp6, фида нет.)
- Где: `create_set_plan.py:676` (feed_alert), + skip-набор `blueprint.py:956` и `routes_jobs.py:275`.
- Root-cause: `_fal_needed = len(feeds)==0 and (want_product or want_master or want_tp3)` — `want_master` (tp6) ошибочно включён в условие «нужен фид». Но master-items ВСЕГДА строятся с `feed_list=[(None,None,None)]` (create_set_plan.py:636) — Мастер кампаний фид НЕ требует (фид нужен только Товарке tp7/product и динамике tp3). Плюс `_skip_feed_types=["product","master"]` — при «Создать без них» из-за товарки МК тоже отбрасывались.
- Решение (2026-07-11):
  - `create_set_plan.py:676` — убрал `want_master` из `_fal_needed` (+ из `will_skip_types` стр.685).
  - `blueprint.py:956` и `routes_jobs.py:275` — `_skip_feed_types = ["product"]` (без "master").
- Статус: 🟡 фикс задеплоен (рестарт direct-create), ждёт прогона — при наборе только с tp6 диалог фида не должен появляться, МК создаются напрямую.

### DMP_MK_KONKURENTY_AUTOTARGET — «МК Конкуренты» схлопывалась в autotarget + авто-коллизия ct (2026-07-12)
- Симптом: у слепка dmp 3 МК (Авто/Ключи/Конкуренты) выходили неотличимыми — все autotarget, «пересечение с авто-слепками». «МК Ключи» при этом работал (пак `dmp/tp6/ct0000/keywords/dmp.txt`=25 фраз, verified LXC101).
- Где: `create_set_plan.py::_emit_struct` (targeting_mode/ct), `create_set_context.py::_slepok_struct_groups` (не пробрасывал gc/mode), `_tp67_keywords_from_real_library._score`; данные — `slepki_structure.json` (dmp tp6), `tp67_real_keywords.json`.
- Root-cause (3 слоя):
  1. «МК Конкуренты» имя → `_tp67_targeting_mode` матчит «конкурент»→`audience`; в `direct_slepok_audiences` для (dmp,dmp,tp6) interest IDs нет → fallback `autotarget`. 69 готовых «ключевых названий конкурентов» в библиотеке осиротели.
  2. `_slepok_struct_groups` брал `code=it["c"]`(="tp6_cpc_site"), а `gc` (кодер) игнорил → все 3 МК уезжали в `ct0000` (нельзя развести источник ключей Ключи vs Конкуренты).
  3. `_score` библиотеки при матче по позиции выбирал ПУСТОЙ decoy-item (dmp position='конкуренты' 0 кл) вперёд реального набора (pos_score приоритетнее ct_score) → [] → autotarget.
  - Доп.: `ct0032/ct0084` (leadgen «Бренд»/«Конкуренты») совпадают с авто gsheet_naming (Changan CS55 / FAW) → `_ag_part1_map` их игнорит → резолвятся в АВТО-имя (авто-бренд в контент). Поэтому ct0084 для Конкуренты НЕЛЬЗЯ.
- Решение (2026-07-12):
  - Код: `_slepok_struct_groups` пробрасывает `gc`+`targeting_mode` item'а в g; `create_set_plan.py` — `targeting_mode = g.get('targeting_mode') or _tp67_targeting_mode(g)` и `cat_ct` берёт `_gc_ct(g['gc'])`; `_score` — `return None` для item без keywords (защита от пустого decoy).
  - Данные: `slepki_structure.json` «МК Конкуренты» → `gc=ct0834` (выделенный leadgen-номер вне авто-пространства) + `targeting_mode:keywords`; `tp67_real_keywords.json` конкурентный item `ct: '' → ct0834`.
  - Комментарий `campaign_naming.py` актуализирован (ct0800–ct0834; ct0032/ct0084 мертвы из-за коллизии).
- Верифицировано (офлайн, факт до/после): Авто→autotarget/ct0000; Ключи→keywords/ct0000 (пак 25); Конкуренты→keywords/ct0834→69 своих ключей; без skip-empty → 0/autotarget (доказана необходимость фикса); pavlov ct0084(FAW) не течёт в dmp (same_slepok выигрывает). py_compile+JSON OK.
- Статус: 🟡 фикс в локальных файлах (Mutagen→LXC101), ждёт рестарта direct-create/worker + боевого прогона dmp на porg-lrfjzcxo.
- ~~НЕ помогло бы: держать Конкуренты на ct0084 — притянул бы авто-бренд FAW (контент/имя).~~
  ⚠️ УСТАРЕЛО 2026-07-18: барьер снят фиксом приоритета `NONAUTO_CT_NAME_PRIORITY` (ниже) —
  у не-авто слепка структура теперь бьёт справочник марок, ct0084 для dmp разрешён (решение Семёна).
- Открытый долг (не блокирует фикс): имена структуры vs coder расходятся (ct0801 структура=«СОЦ сети» vs coder=«Идентификация»); жёсткий slepok-фильтр в `_score` для не-авто (анти-bleed).
- ✅ ЗАКРЫТО 2026-07-18 (naming gap): ct0822–ct0833 в `public.leadgen_ct_naming` уже были, не хватало ТОЛЬКО `ct0834` → зарегистрирован как «Конкуренты» (36→37 строк). Имена ct0800–ct0834 сверены со структурой `slepki/dmp.json` 1:1. Проверено: `_ag_part1_map()['ct0834']='Конкуренты'`, `_coder_name_real_brand('Конкуренты')=False`, `_brand_ct_from_coder(ct0834)=('','')` → в бренд/контент НЕ течёт, только подпись ct. Кэш `_AG1_NAME_CACHE` — на процесс: `direct-slepki` перезапущен, `direct-create`/`worker` подхватят на ближайшем рестарте.

### DMP_UI_TP6_LIES — дерево слепка врало по числу кампаний и по ключам tp6/tp7 (2026-07-18)
- Симптом (страница `/direct/automation/slepki`, слепок dmp): (A) в шапке tp6 «1 кампания» при ТРЁХ строках-кампаниях в дереве; (B) карточка «МК Конкуренты» показывала плашку «Автотаргетинг — ключевых слов в паке нет», хотя в кабинет реально уезжают 69 фраз.
- Где: `static/direct/slepki_ui.js` (счётчик плоской ветки tp), `slepki_editor.read_group_keywords` ← `routes_slepki_edit.py:/api/slepki/keywords`.
- Root-cause: (A) `campN=(t.groups||[]).length` считал ГРУППЫ, а `slepkiGroups` в tp6/tp7 рисует строку на КАЖДЫЙ item (группа «МК» — контейнер на 3 кампании). У авто-слепков items ровно по 1 на группу, поэтому баг не всплывал. (B) карточка читала ТОЛЬКО M3-пак, а создание при пустом паке берёт ключи фолбэком `create_set_context._tp67_keywords_for` (библиотека `tp67_real_keywords.json`) — два разных источника на один и тот же вопрос.
- Решение (2026-07-18): (A) `_slFlatCampCount(groups,tpn)` рядом с `slepkiGroups` — счёт по тому же правилу, что и рендер (tp6/7 → items, иначе → группы); применён и к splits-ветке tp3/6/7. (B) `read_group_keywords` при пустом паке и tp6/tp7 зовёт ТУ ЖЕ функцию, что создание, и отдаёт `kw_source=pack|real_library`; UI подписывает источник и НЕ подставляет библиотечные ключи в textarea правки (иначе «сохранить» вкопировало бы их в пак).
- ⚠️ ГЕЙТ РЕЖИМА обязателен (найден на прогоне): без него фолбэк показывал 76/178 ключей на АВТОТАРГЕТ-строках pavlov/terehov — те в кабинет не уедут (создание берёт ключи только при `_want_keywords`). Фолбэк включается лишь когда `_tp67_targeting_mode({"name": position})=="keywords"`; `position` пуст → фолбэка нет (прежнее поведение).
- Верифицировано (LXC101, прод-венв, реальный пак): dmp/tp6/ct0834 `kw_source=real_library positive=69`; dmp/tp6/ct0000 и pavlov/terehov автотаргет — `pack`, без изменений; terehov «ТК Ключи - КС» → 178; tp2 не затронут. Счётчик: прогон патченной JS-функции по всем 184 плоским узлам — изменились РОВНО 2 (dmp/tp6 1→3, tumashenko/Монобренд/tp7 2→4, оба — многоitem'ные группы), остальные 182 бит-в-бит прежние.
- Статус: 🟡 ждёт живой приёмки на странице (код задеплоен Mutagen, `direct-slepki.service` перезапущен, active, err-журнал пуст). Проверять глазами: dmp → tp6 «3 кампании» + карточка «МК Конкуренты» с 69 ключами и подписью источника.
- НЕ помогло ранее: — (первая правка этого класса). ⚠️ Гипотеза «просто отдавать фолбэк всегда для tp6/tp7» — ОПРОВЕРГНУТА прогоном (врёт на автотаргет-строках), не повторять.

### CALLOUTS_NAMEERROR_TIME — уточнения создаются, но НЕ привязываются (NameError) (2026-07-12)
- Симптом: объявления выходят без уточнений (callouts); в API остаются осиротевшие Callout-объекты (созданы, но не привязаны).
- Где: `create_set_assets.py:687` — `time.sleep(_AC_BATCH_SLEEP)` в пуле заливки уточнений (`adextensions.add`).
- Root-cause: `time` НЕ импортирован (в файле только `import re`) и не в DI → `NameError: name 'time' is not defined` после `adextensions.add`. Вызывающие ветки глушат исключение → кампания достраивается без callouts, созданные Callout-объекты не используются. (Находка Семёна, воспроизведено.)
- Решение (2026-07-12): `create_set_assets.py` — добавлен `import time` в шапку. py_compile OK.
- Статус: 🟡 фикс в файле (Mutagen→LXC101), ждёт рестарта worker + прогона с уточнениями.

### DMP_TP2_PACK_EMPTY_GATHER_SSH_M3 — «пак M3 пуст», tp2 dmp падают в defer (2026-07-12)
- Симптом: `live_verification` → 5× `RESULT_FAILED` «tp2(куки): пак M3 пуст/недоступен (M3_alive=True) — отложено на докрутку»; tp2-кампании dmp не создаются. (Вопрос Семёна: «причём тут M3, если всё грузим с 101?».)
- Где: `create_set_tp1_builders.py::_tp1_pack_groups:1419` → `kp.gather` → `kontent_pack.py::gather:1394`.
- Root-cause: `gather` читала пак ТОЛЬКО по `ssh (_M3_SSH) к M3_PACK_ROOT` — удалённый M3-relay. Редеплой кодера (ct0800–ct0834) прошёл лишь в ЛОКАЛЬНОЕ зеркало `/opt/neuro_content_local` (что читает `read_keywords`), а удалённый M3 остался старым (ct0001–ct0034). → `gather` отдавала ct0001–34, план требовал `only_cts=ct0800+` → пересечение ПУСТОЕ → 0 групп → defer. `refresh_index` не помогал: `gather` индекс не использует, ходит на M3 напрямую.
  - Доказано на LXC101: `gather` (ssh M3) = ct0001–34; `_GATHER_PY` локально по зеркалу = ct0032/ct0084/ct0800–ct0821 (правильно).
- Решение (2026-07-12): `kontent_pack.py::gather` — local-first (как `refresh_index`): при активном зеркале запускать `_GATHER_PY` ЛОКАЛЬНО через `_ensure_local_shim()`, ssh M3 — фолбэк.
- Верифицировано: после деплоя `gather("dmp","dmp","tp2")` = 24 cts (ct0800–ct0821 + ct0032/ct0084); md5 Mac==LXC101; worker перезапущен.
- Статус: ✅ ПОДТВЕРЖДЕНО МАСШТАБОМ (2026-07-14, 4 акка y-direct-victory). tp2 dmp создаются (не defer), ключи LIVE=2386/0 пустых из 68 на КАЖДОМ из 4 (build==live). Десинк ct пака (пак/M3 на старом ct0001–34 vs структура ct0800+) закрыт: пак наполнен в leadgen-нумерации, gather читает local-first зеркало 101. **Переносимо:** пак-папки dmp на M3/зеркале ДОЛЖНЫ быть в ct0800–ct0833 (leadgen-неймспейс, +799 от старой авто-нумерации), иначе пересечение с `only_cts` плана = пусто → 0 групп → defer.
- ✅ Долг закрыт 2026-07-12: пак dmp/tp2 наполнен из выгрузок кабинета — 34 ct (ct0800–ct0833), 1204 ключа, вкл. ct0822–ct0833.

### DMP_SITELINKS_AUTO — быстрые ссылки dmp выходят авто-сферы (2026-07-12)
- Симптом: у B2B-слепка dmp сайтлинки в кабинете — авто («Первый взнос 0₽/автокредит», «Оценка авто в трейд-ин», «КАСКО на 1 год», «Тест-драйв»), а не B2B.
- Root-cause: `direct_slepok_content` (dmp/campaign) хранил sitelinks СТРОКАМИ; `_common_sitelinks_fast` (create_set_feed_builders.py:74) и `_slepok_campaign_content` (blueprint.py:6707) берут только `isinstance(s,dict)` → строки отсеивались → [] → фолбэк на авто-ассеты аккаунта.
- Решение: `ai_content.py:_slepok_content_get` нормализует строки→dict (+seed из `AGENT_ADS["dmp"]["sitelinks"]`); в БД `direct_slepok_content` записаны 8 B2B-сайтлинков (title+description).
- Статус: 🟡 задеплоено, ждёт прогона — сайтлинки dmp B2B, без авто-лексики.

### DMP_GROUP_NAMES_AVTO — почти все группы tp2 названы «— Авто» (2026-07-12)
- Симптом: в кабинете dmp почти все группы = `ct08XX_… — Авто` (дубли), лишь единицы с верным именем.
- Root-cause: `_tp1_pack_groups` брал `raw_brand = ct_name.get(ct) or ct_model.get(ct) or ct`; при пустом leadgen фолбэк уходил в `feeds_ct_model` (авто-фид) = «Авто».
- Решение: (1) `leadgen_ct_naming` выровнен по выгрузке (все ct0800–ct0833 = имена тем); (2) для не-авто имя = leadgen → структура `t` (выгрузка) → ct, авто-фид НЕ используется — `create_set_tp1_builders.py::_struct_ct_names` + правка `raw_brand` в `_tp1_pack_groups` и `_build_tp1_from_pack`.
- Верифицировано (LXC101): `_struct_ct_names("dmp","dmp")`=36 ct верных имён; авто-слепки → {}.
- Статус: 🟡 задеплоено, ждёт прогона.
- ДОФИКС (2026-07-14, token-путь + cookie gc-хвост): пилот показал, что имена dmp tp2 всё ещё одинаковые `ct08NN_..._g00 — Авто`. Прежний фикс покрыл только cookie `_tp1_pack_groups`/`_build_tp1_from_pack`; **token-путь `_build_text_from_pack` (create_set_text_builders.py) НЕ был покрыт**, а cookie ещё оставлял gc-префикс. Root-cause token: `model = _valid_pack_brand_name(ct, ct_name.get(ct) or ct_model.get(ct) or ct) or "Авто"` — для dmp-тем (не марок) → "" → фолбэк «Авто» у всех ct; имя = `_text_group_name(ct,r_code,model)` = `ct08NN_..._g00 — Авто`. Фикс: гейт `_struct_names = _struct_ct_names(slepok,site_type)` (непустой ТОЛЬКО у auto:false=dmp). Token (`create_set_text_builders.py` ~346/361-403): при `_struct_names` → `raw_name=ct_name.get(ct) or _struct_names.get(ct) or ct` (НЕ падать в ct_model), `brand=_valid_pack_brand_name(...)` (без «Авто»-фолбэка → тема→"" → «Авто» не течёт в заголовки: title-seed пуст, `_rsya_titles` ведёт B2B-корпусом), `display=_pack_group_display_name(...)` (тема), имя группы = `display` (чистая тема, без gc/«Авто»); авто-ветка (`else`) — прежний `ct_model`+«Авто»+`_text_group_name`. Cookie (`create_set_tp1_builders.py` ~1505): при `_struct_names and _is_search_tp` → имя = `group_label` (снят gc-префикс), авто-ветка не тронута. Wiring: wrapper `_struct_ct_names` + `_struct_ct_names`/`_pack_group_display_name` добавлены в text-deps (`automation_runtime.py`). Backup `.editbak.20260714_105001` ×3. py_compile OK; офлайн-гейт: dmp→36 тем (Идентификация/СОЦ сети/…), terehov/pavlov/scherbakova→{} (авто целы).
- Статус: ✅ ПОДТВЕРЖДЕНО МАСШТАБОМ (2026-07-14, 4 акка). Нейминг групп tp2 dmp = **34 distinct тематических имени** (Идентификация/СОЦ сети/Номер·телефон/Вордстат/…), 0 с ct-кодом/gc-хвостом/«— Авто» на каждом из 4 акков (прямой дамп adgroup_name). Заголовки dmp без «Авто». Авто-слепки не тронуты. **Переносимо:** имя группы не-авто = кодер (`leadgen_ct_naming`) → тема из слепка `t` → ct; авто-фид `feeds_ct_model` и «Авто»-фолбэк для `"auto": false` НЕ применяются (token-путь `_build_text_from_pack` + cookie `_tp1_pack_groups`, оба покрыты).
- НЕ помогло ранее: первый фикс `DMP_GROUP_NAMES_AVTO` (2026-07-12) закрыл только cookie-нейминг, но не token-путь `_build_text_from_pack` (пилот 2026-07-14 всё ещё дал «— Авто») и оставлял gc-префикс в cookie.

### DMP_PAY_CHECKBOX — тип оплаты cpc/cpa из split.pay, а не по галочке (2026-07-12)
- Симптом (правило Семёна): тип оплаты dmp копировался из `split.pay`; нужно как в авто — по галочке «под стиль сайта», едино для ВСЕХ слепков.
- Решение: `create_set_plan.py` — единый `pays = ["tcpa"] if no_cpa else ["tcpa","cpa"]` (`no_cpa=body["n"]`); dmp tp2-сплиты и МК используют `pays` вместо split.pay. Активна → cpc+cpa; снята → только cpc. Заодно починен латентный баг: раньше `no_cpa` не влиял на tp2/tp4/МК (pays был жёстко `[tcpa,cpa]`).
- Статус: 🟡 задеплоено — проверить: активна → dmp 16 РК; снята → 8 РК.

### TEREHOV_TERM_AND_NO_CITY — «Обмен авто» vs «Трейд-ин» + город в заголовках terehov (2026-07-11)
Два контент-изменения по тон-оф-войс terehov (решение Семёна #14/#15).

**#14 — Термин «Обмен авто» (не «Трейд-ин») для terehov.**
- Root-cause: terehov — разговорный/уличный стиль («Б/У авто», «за 1 день», «Звоните!»); «Трейд-ин» = отраслевой англицизм (формальнее), «Обмен авто» = повседневный русский (органичнее тону).
- Решение: `ai_agents.py:AGENT_ADS['terehov']` — замена единообразно:
  - texts[1]: «Оценим дорого в трейд-ин» → «Обменяем ваше авто выгодно»
  - sitelinks[5]: `("Трейд-ин выше рынка", ...)` → `("Обмен авто выгодно", ...)`
- Другие слепки: НЕ тронуты (pavlov/scherbakova/karavaev/gordeeva — «Трейд-ин выше рынка» остался).

**#15 — Город НЕ вставляется в заголовки terehov.**
- Root-cause: `_brand_title_set` и `_title_from_template` строили «{brand} в {city_loc}.» для ВСЕХ слепков.
  Для terehov — нежелательно (разговорный стиль без привязки к городу в заголовке).
- Решение (три файла, только slepok=="terehov", другие не затронуты):
  1. `text_gen.py` — `_SLEPOKS_NO_CITY_TITLES = frozenset({"terehov"})` + `slepok=""` параметр
     в `_title_from_template` и `_rsya_titles`; guard `if slepok in _SLEPOKS_NO_CITY_TITLES: city = ""`.
  2. `create_set_text_builders.py:363,373` — передаём `slepok=slepok` в `_title_from_template` и `_rsya_titles`.
  3. `ai_agents.py:build_titles_messages` — добавлен `_akey_early` + `_terehov_no_city_rule`:
     явный запрет в LLM-промпте для теrехова «⛔ НЕ упоминай ГОРОД в заголовках».
- ⚠️ НЕ покрыто (инфра-граница): `create_set_tp1_builders.py:709,714,1472,1478` — тоже вызывают
  `_title_from_template(brand, city)` и `_rsya_titles(brand, city, ...)` без slepok= → для tp1-пути
  город ещё попадает в шаблоны terehov. Нужна правка в `create_set_tp1_builders.py` через main-сессию.
- Верификация: py_compile OK (все 3 файла); pyflakes — 0 новых undefined-name.
- Чинит уже созданные РК: НЕТ. Применяется при следующем прогоне create_set.
- Статус: 🟡 код на Mac. НЕ деплоено (Mutagen засинкает, рестарт direct-create.service — отдельно).

### DEGRADATION_4FIX — деферред-потеря/ложный-дефер/3-джобы на мульти-слепок прогоне (2026-07-11)
Четыре точечных фикса по read-only root-cause (прогон деградировал на мульти-слепок single-login).
- **ФИКС 1 (косметика диагностики).** `create_set_tp1_builders.py:_build_tp1_from_pack` логировал
  захардкоженный литерал «ключами scherbakova» для ЛЮБОГО слепка → путал диагностику. Заменил на
  реальный `{key}` (M3-lookup-ключ слепка) в лог-строке; 2 комментария → «ключи слепка».
- **ФИКС 2 (потеря деферреда).** `blueprint.py:_deferred_save` дедупил остаток по ИМЕНИ item
  (`body->'items' @> [{name}]`) в рамках login. Имена слепок-АГНОСТИЧНЫ (кодируют tp/сегмент/город,
  НЕ слепок) → defer одного слепка (zubakin) схлопывался в уже созданный deferred другого (gordeeva)
  по совпавшему имени и ТЕРЯЛСЯ. Дополнил ключ дедупа слепком: `AND COALESCE(body->>'agent','')=%s`
  (login уже в scope; COALESCE('') покрывает легаси-строки без agent → обратная совместимость).
- **ФИКС 3 (3 джобы на логине).** `blueprint.py:_resume_daemon_loop` брал до 5 waiting-строк и стартовал
  `_resume_one_deferred`→`_job_new(priority=True)` для каждой БЕЗ проверки активного create → 3 джобы
  разом на одном логине (гонка/дубли/конфликт баллов). Добавил гард: пропуск строки, если у логина
  есть активная create-джоба (`_job_db_active_by_login` = queued/running/claimed/resumed) ИЛИ resume
  для него уже поднят в этом батче (`_busy_launched`). Занят → `continue` (resume_at НЕ сдвигаем,
  строка остаётся waiting → следующий поллинг).
- **ФИКС 4 (ложный M3-glitch defer).** `create_set_tp1_builders.py:_pack_read_glitch` детектил «сбой
  чтения M3» по пробе СОСЕДНЕГО tp: если у слепка сосед (tp2) ЛЕГИТИМНО пуст (gordeeva) → probe пуст
  → ложно «M3 down» → вечный defer сегмента, пака которого нет в принципе. `gather()` возвращает `{}`
  и при сбое инфры, и при легит-пустом паке — амбигуитет. Добавил `kp.m3_reachable()` (real-time
  health-probe M3-relay ТЕМ ЖЕ ssh-транспортом, тривиальный `true`, без обхода каталогов → не виснет).
  Новый дискриминатор: сосед непуст → инфра жива, целевой пак легит-пуст → False; сосед пуст →
  `not m3_reachable()` (relay отвечает → нет пака → False; relay мёртв → реальный сбой → True/дефер).
- Статус: 🟡 код на Mac, py_compile OK (все 3 файла). НЕ деплоено (Семён катнёт после ревью; воркер стоит).
- Live-проверка direct_verifier: мульти-слепок single-login прогон (zubakin+gordeeva на одном логине) →
  оба слепка сохраняют свои deferred (нет потери по имени); gordeeva-сегмент без пака НЕ уходит в
  вечный defer; на логине с активным create демон НЕ поднимает вторую docrutka-джобу.
- НЕ помогло ранее: — (уточнение существующей анти-дуп логики от 2026-07-07, не откат).

### NO_IMAGES_COOKIE_TP1_FRESH_CT — свежий ct на cookie-пути tp1 создавался без картинок (2026-07-11)
- Симптом: NO_IMAGES_LIVE(tp1, систематичен) — объявления РСЯ без картинок на cookie-пути (когда
  исчерпан лимит v5=152 и пользователь согласился создавать по куке). Особенно первая РК бренда в
  аккаунте (свежий ct).
- Где: `create_set_tp1_builders.py:_create_tp1_via_cookie` (перед `gc.create_full`).
- Root-cause: пред-create резолвинг картинок шёл ТОЛЬКО через `_grid_account_image_hashes(login)`
  (`_img_map`) — хэши УЖЕ привязанных к объявлениям картинок аккаунта. Для свежего ct basename нет в
  `_img_map` → `_tp1_pack_groups` оставлял `g["image_hashes"]` пустым → `create_full` →
  `build_ad(image_hashes=[])` → объявление БЕЗ картинок. Пост-create Grid-repair (блок ~1830,
  `_grid_update_adaptive_ads` full-replace) существует, но НЕНАДЁЖЕН (STATE:25 «auto-repair grid,
  систематичен») → картинки часто не «прилипали». `_preupload_tp1_images` не спасает: льёт в
  библиотеку аккаунта, а этот путь читал картинки-на-объявлениях.
- Решение (2026-07-11): в `_create_tp1_via_cookie` ПЕРЕД `create_full` добавлена пред-заливка —
  собрать `image_paths` всех групп, `_parallel_upload_images(gf.get_grid_client(login), login,
  paths, account_map=_img_map)` (Grid `upload_image` — БЕЗ баллов, допустим при 0 units; НЕ v5
  `adimages.add`=152), проставить `g["image_hashes"]` (≤5, дедуп) для всех групп. Так объявления
  создаются СРАЗУ С картинками, не завися от флакового пост-create repair. Зеркалит token-path
  Фаза 3.4 (`_build_tp1_adgroups:388`). Правка ТОЛЬКО в `_create_tp1_via_cookie`, token-path и
  `_parallel_upload_images` не тронуты. best-effort try/except — не роняет создание кампании.
- Верификация (статическая): py_compile OK; новые имена (`_gc_img_pre`/`_uploaded_pre`/`_osu`/
  `_all_paths`/`_iue`) pyflakes НЕ помечает; прочие «undefined» — DI-инъекции `configure()`.
- Статус: 🟡 код на Mac, py_compile OK. НЕ деплоено (Семён катнёт на direct-worker.service после ревью).
- Live-проверка direct_verifier: cookie-путь tp1 на свежем аккаунте/бренде → в кабинете у
  комбинаторных РСЯ-объявлений есть imageHashes (картинки видны сразу после создания, без ожидания
  пост-create repair).
- НЕ помогло ранее: пост-create Grid-repair (`_grid_update_adaptive_ads`, коммит 402949f 2026-07-10) —
  добавлял картинки ПОСЛЕ create через UpdateAdaptiveTextAds full-replace, но флаковал →
  NO_IMAGES оставался систематичным. `_preupload_tp1_images` (в библиотеку) — этот путь её не читал.

### TP7_GOALID_FROM_PAYLOAD — Товарка/SMART, goalId=0 при отсутствии счётчика (2026-07-11)
- Симптом (историч.): tp7 UAC-товарка падала «goalId=0 / ошибка 4000» когда `metrika_goals`
  (FOREIGN-таблица) не отдавала счётчик/цель для логина.
- Где: tp7, путь UAC/cookie, `create_set_metrika.py:prepare_metrika` → `create_set_orchestrator`
  (counter_id/goal_id) → `create_set_master_product.run_master_product_item`
  → `MasterCampaignSpec(counter_id,goal_id)` → `campaign.create_master_campaign`.
- Root-cause: цель для конверсионной стратегии товарки берётся из счётчика клиента; при пустом
  `metrika_goals` goal=0 → Яндекс отклоняет.
- Решение (Семёна): `prepare_metrika` принимает `counter_id`/`goal_id` из PAYLOAD напрямую
  (`api/create_set_async` → normalize_create_set_input → prepare_metrika), только при их отсутствии
  идёт в `metrika_goals`. Передача счётчик+цель в body обходит FOREIGN-таблицу.
- Статус: ✅ подтверждено прогоном 2026-07-11. porg-vfdnaolu, counter=110499992/goal=579905467
  в payload → 3 tp7-товарки (BAIC/Changan/Chery, ids 712717953/955/958) созданы created=3/failed=0/
  errors=0, БЕЗ goalId=0. Сырой UAC-детейл: **goal_id=579905467** (≠0), feed_id=3560490, ecom=true
  на всех 3. Live-verifier: status=pass, 0 issues.
- ⚠️ Смежная находка (НЕ баг создания, но чинить): у porg-vfdnaolu 8 фидов, НИ ОДИН не в allow-list
  «Глобальных правил» (`_allowed_feed_keys`): у аккаунта префикс `used-*` и `yandex-catalog*.xml`,
  нет `/yandex.xml` и нет `yandex-catalog-model-design-custom-name.xml`. → штатный set_plan
  (single_feed) даёт 0 product-items → до UAC-создания НЕ доходит. Тест goalId прошёл только с
  точечным in-process разрешением ОДНОГО реального каталог-фида (3560490 yandex-catalog.xml).
  Для боевого tp7 на used-car аккаунтах нужен либо фид из allow-list, либо расширение allow-list.
- ⚠️ Латентный баг (masked): `create_set_plan.py:277` читает v5 `feeds` с FieldNames `["Id","Name",
  "SourceType","Url"]` — `Url` НЕ валиден (v5 требует Id/Name/BusinessType/SourceType/FilterSchema/…),
  вызов ВСЕГДА возвращает «Некорректный запрос» → молчаливый фолбэк на Grid `_grid_feeds`. Не ломает
  (Grid работает), но v5-путь фидов мёртв. Убрать `Url` из FieldNames (URL берётся из Grid).

### DMP_FULL_B2B_PIPELINE — весь пайплайн dmp генерировал авто-кредитный контент (2026-07-11)
- Симптом: для `site_type=="dmp"` (B2B-лидоген, dmp-ai.ru) все объявления содержали
  «кредит/платёж/₽/мес/трейд-ин/КАСКО» — даже при исправном паке. Gate `validate_create_set_content`
  блокировал запуск с «нет шаблонных текстов для типа сайта dmp».
- Где: 6 мест авто-кредитного кровотечения + 1 gate-блокер.
- Root-cause (все 7 проблем):
  1. **Gate-блокер** `validate_create_set_content` (create_set_account.py:59) — 0 строк для
     site_type='dmp' в `public.direct_ad_templates` → "нет шаблонных текстов".
  2. **`build_texts_messages`** — нет dmp-guard; авто-кредитный промпт строился для всех.
  3. **`build_sitelinks_messages`** — нет dmp-guard; авто-сайтлинковый промпт для всех.
  4. **`_title_ok`/`_text_ok`** в `create_content.py` (оба набора: closure + `_accept_title`/`_accept_text`)
     — требовали `_DIRECT_CREDIT_RE` (кредитный угол) → весь B2B-контент блокировался.
  5. **`assemble_campaign`** (`ai_agents.py`) — внутренние `_title_ok`/`_text_ok` тоже требовали
     кредитный угол → corpus dmp-агента отбрасывался.
  6. **`_final_fill_campaign_content`** — все fillers авто-кредитные; `_credit_offer_ok_line`
     блокировала B2B.
  7. **`sitelink_bank_for("dmp")`** — возвращал `COMMON_SITELINK_BANK` (авто); `_sitelink_bucket_limits`
     ставил `"other"=1` → все 8 B2B-сайтлинков (попадающих в «other»-корзину) срезались до 1.
- Решение (2026-07-11):
  - **DB**: 25 строк (12 title, 5 text, 8 sitelink) добавлены в `public.direct_ad_templates` для
    site_type='dmp' (idempotent INSERT, содержимое — B2B-лидоген без авто/кредита).
  - **`ai_agents.py`**:
    - `AGENT_ADS['dmp']`: добавлены 8 B2B-сайтлинков (ранее отсутствовали).
    - `sitelink_bank_for`: `if st == "dmp": return [8 B2B сайтлинков]`.
    - `_sitelink_bucket_limits`: `if st == "dmp": return {k: 8 for k in limits}` — снимает
      ограничение «other=1».
    - `assemble_campaign`: добавлен `_is_dmp = (st == "dmp")`, `_dmp_b2b_re`; `_title_ok`/`_text_ok`
      теперь проверяют B2B-маркер (лид/контакт/клиент/горяч…) вместо кредитного угла для dmp.
    - `build_texts_messages`: dmp-guard → early return с B2B-текстовым промптом, явный `⛔ ЗАПРЕЩЕНО:
      кредит/₽/мес/трейд-ин/КАСКО/авто/дилер`; corp = `AGENT_ADS['dmp']['texts']`.
    - `build_sitelinks_messages`: dmp-guard → early return с B2B-сайтлинковым промптом;
      corp = `sitelink_bank_for("dmp")`.
  - **`create_content.py`**:
    - `_is_dmp = (st == "dmp")`, `_DMP_B2B_UTP_RE` — добавлены после `_new_only_site`.
    - `_title_ok`, `_text_ok` (closure): `if _is_dmp: return _DMP_B2B_UTP_RE.search(t)` вместо
      кредитных проверок.
    - `_accept_title`, `_accept_text` (LLM-output-фильтр): аналогично — `if _is_dmp` ветка
      с `_DMP_B2B_UTP_RE`, иначе авто-путь без изменений.
    - `_final_fill_campaign_content`: `if _is_dmp:` ветка с 12 B2B-заголовками, 5 B2B-текстами,
      8 B2B-сайтлинками (все ≤56/81/30+60 символов; цифры в каждом заголовке).
    - `_credit_offer_ok_line`: `if _is_dmp: return True` — B2B не требует кредитный угол.
  - Все правки под `st == "dmp"` / `_is_dmp` — авто-слепки не затронуты.
- Верификация (статическая):
  - py_compile OK на ai_agents.py + create_content.py; pyflakes 0 undefined-name.
  - Тест фильтров: 8/8 dmp title fillers прошли `_DMP_B2B_UTP_RE`, 0 авто-кредита; 5/5 dmp texts
    прошли, все ≤81c; все авто-заголовки/тексты заблокированы dmp-фильтром (test 2-3 PASS).
  - Длины: все sitelink title 22–28c (≥MIN_ACCEPT=18, ≥TARGET_MIN=22), desc 50–53c (≥50).
  - DB: INSERT OK rows=25; SELECT count=25 (12+5+8); site_type='dmp' ✓.
- Чинит ли delayed content_repair уже созданные РК: НЕТ. dmp-прогонов ещё не было.
  Фикс применяется при следующем запуске create_set для dmp.
- Статус: 🟡 код на Mac + DB ✅. Mutagen засинкает, НЕ деплоено (рестарт сервиса — отдельно).
- НЕ помогло ранее: частичный dmp-guard в build_titles_messages (добавлен 2026-07-10) —
  блокировал авто только в заголовках; тексты/сайтлинки/фильтры оставались авто.

**Round 2 — доминантный путь `_gen_campaign_content`/`_rsya_titles`/`_upgrade_credit_titles` (2026-07-11):**
После Round 1 диагностика показала: titles гибрид авто+B2B, texts 100% авто, sitelinks 100% авто.
Дополнительные корни:
  1. `_accept_title`/`_accept_text` + `_title_ok`/`_text_ok` (create_content.py) — branch order: brand-check ДО dmp-check
     → B2B-контент LLM и корпус agentads['dmp'] отвергались при наличии бренда в ct.
  2. `assemble_campaign._title_ok`/`._text_ok` (ai_agents.py) — `_brand_ok(c) AND B2B` → корпус dmp сбрасывался, emergency fallback без dmp-guard.
  3. `sitelink_fillers` (create_content.py, ~строка 723) — авто-список overwrite dmp B2B sitelinks.
  4. `_needs_credit_title_upgrade` (create_set_assets.py) — B2B без кредита → `_upgrade_credit_titles` навешивал авто.
  5. `_rsya_titles` (text_gen.py) — нет dmp-guard → авто brand-title-set и _GENERIC_TITLE_FILLERS.
Решение (2026-07-11):
  - `create_content.py` (7 правок): dmp-check ПЕРВЫМ во всех 4 функциях; `_brand_ok_line` True для dmp;
    sitelink_fillers под `if not _is_dmp`; stop-words в `_take_titles`/`_take_texts`/`_take_sitelinks`.
  - `ai_agents.py` (3 правки): убран `_brand_ok(c)` из dmp-ветки; emergency fallback с B2B фолбэком.
  - `create_set_assets.py` (1 правка): `_DMP_B2B_TITLE_RE` + guard в `_needs_credit_title_upgrade` → False при B2B-маркерах.
  - `text_gen.py` (1 правка): `_rsya_titles` — ранний return для dmp с `AGENT_ADS['dmp']` + B2B fillers.
Верификация (2026-07-11, статическая):
  - py_compile OK на всех 4 файлах; pyflakes 0 новых undefined-name.
  - `_needs_credit_title_upgrade(B2B_TITLES)` → False; `(AUTO_TITLES)` → True. PASS.
  - `_rsya_titles(site_type="dmp")` → 7 B2B-заголовков, 0 авто-контента. PASS.
  - `AGENT_ADS['dmp']`: 4 title / 3 text / 8 sitelink — все B2B-маркеры. PASS.
- Статус: 🟡 Round 2 код на Mac. НЕ деплоено (Mutagen засинкает, рестарт — отдельно).

**Round 3 — UAC/master-путь `create_set_master_product.py` (tp6/tp7) (2026-07-11):**
Дополнительный корень: tp1/tp2-контент-путь закрыт Rounds 1-2, но tp6 МК (UAC-путь) строился
через `create_set_master_product.py` с прямым использованием `_GENERIC_AT_TITLES` как базы.
Root-cause:
  1. `create_set_master_product.py:141-145` (до правки) — `else` ветка tp6 ct0000: `title_primary = _GENERIC_AT_TITLES`; B2B-корпус шёл только в `title_supp` добавкой.
  2. `_has_number` (number-gate) :178 — резал B2B-строки без цифр («Получайте контакты потенциальных клиентов…»).
  3. `if not c_brand: _x_base = _GENERIC_TEXT_FILLERS` :220 — авто-тексты вместо B2B-корпуса.
  4. `_GENERIC_TITLE_FILLERS`/`_GENERIC_TEXT_FILLERS` в пуле `_fill_variants` — авто-добор.
  5. `_fallback_master_titles` — авто-строки («Авто в кредит», «КАСКО»…) при <5 заголовков.
Решение (2026-07-11):
  - `blueprint.py:7290` — добавлен `"_slepok_is_auto"` в `_master_product_deps()`.
  - `create_set_master_product.py`:
    - :81 — `_slepok_is_auto = deps['_slepok_is_auto']`
    - :85 — `_is_non_auto = not _slepok_is_auto(agent or "")`
    - :151-163 — `else` ветка tp6 и `elif is_product` ветка tp7: при `_is_non_auto` база = B2B-корпус `_sc["titles"]`, авто-дженерики не подмешиваем.
    - :188-190 — `_title_fill_pool`: для не-авто исключён `_GENERIC_TITLE_FILLERS`.
    - :198 — number-gate заголовков: `not _is_non_auto and not _has_number(_t)`.
    - :239-245 — тексты: при `_is_non_auto` `_x_base = _b2b_texts or _GENERIC_TEXT_FILLERS`.
    - :249-251 — `_text_fill_pool`: для не-авто исключён `_GENERIC_TEXT_FILLERS`.
    - :260 — number-gate текстов: `not _is_non_auto and not _has_number(_x)`.
    - :regen-loop (titles/texts) — аналогично.
    - :411-412 — fallback `_fallback_master_titles` guard: `not _is_non_auto`.
    - :413-417 — текстовый fallback `_txt_fallback = [] if _is_non_auto else list(_GENERIC_TEXT_FILLERS)`.
  - Авто-слепки: `_slepok_is_auto(agent)=True` → `_is_non_auto=False` → все ветки авто-пути без изменений.
- Верификация (статическая, 2026-07-11):
  - py_compile OK (create_set_master_product.py + blueprint.py).
  - pyflakes: create_set_master_product.py — 0 предупреждений; blueprint.py — 0 undefined-name.
- Чинит ли уже созданные РК: НЕТ. Применяется только при следующем прогоне create_set для dmp.
- Статус: 🟡 код на Mac. НЕ деплоено (Mutagen засинкает, рестарт direct-create.service — отдельно).

**Round 4 — тексты tp2 Поиска: `_rsya_texts()` в `text_gen.py` (2026-07-11):**
Root-cause: `_rsya_texts()` не имела dmp-ветки совсем. Вызывается из `_build_text_from_pack` (create_set_text_builders.py:374)
для КАЖДОЙ ct-группы tp2. Без ветки функция:
  1. Запускала `_brand_text_set(brand=«BAIC», city)` → «Купить BAIC в кредит. Первый взнос 0 ₽ и КАСКО...» и т.п.
  2. Добивала `_RSYA_TEXT_POOL` — пул авто-УТП («Господдержка и выгодный кредит...»).
  3. Применяла `pad_tails` — «Одобрение за 30 минут», «КАСКО в подарок», «Трейд-ин выше рынка».
Итог: 3 текста = 100% авто-кредитный контент, даже если incoming уже B2B.
Решение (2026-07-11): `text_gen.py:642-657` — ранний return dmp-ветки по образцу `_rsya_titles` (строки 1034-1052):
  - `if site_type == "dmp":` — lazy import ai_agents; corpus из `AGENT_ADS['dmp']['texts']` (3 B2B-текста);
  - 5 B2B-текстов-филлеров (контакты/лиды/клиенты, без авто-лексики);
  - `dict.fromkeys` дедуп incoming + corpus + fillers → возврат cap=3 B2B-текстов.
  - Авто-пути (`_brand_text_set`, `_RSYA_TEXT_POOL`, `pad_tails`) — пропускаются целиком для dmp.
  - Авто-слепки: ветка `site_type == "dmp"` не срабатывает → без изменений.
- Верификация (2026-07-11):
  - py_compile text_gen.py OK; pyflakes 0 ошибок.
  - Логика-тест: dmp incoming=["Предоставим контакты..."] → 3 B2B-текста, авто-лексика отсутствует (PASS).
  - dmp пустой incoming → 3 текста из corpus (PASS). Дедуп → 0 дублей (PASS). Авто site_type='new' → прежний путь (PASS).
- Чинит ли уже созданные РК: НЕТ. Только будущие прогоны create_set для dmp.
- Статус: 🟡 Round 4 код на Mac (text_gen.py:642-657). НЕ деплоено.

### DMP_TITLES_AUTO_CREDIT_BLEED + CT0000_BRAND_HALLUCINATION + KVIZ_BU_LEAK + TITLES_SHORT_BUDGET (контент-тест #51, 2026-07-10)
Четыре бага генерации заголовков/текстов, найденные боевым M3. Все в `ai_agents.py`.

#### DMP_TITLES_AUTO_CREDIT_BLEED — dmp тянул авто-кредитные правила
- Симптом: для `site_type=="dmp"` генерировались «BAIC в наличии. От 9 000 ₽/мес в кредит» — авто-кредит в B2B-лидогене.
- Root-cause: `build_titles_messages` строил единый авто-кредитный промпт (ОБЩАЯ РАМКА: кредит + АВТОМОБИЛИ) для ВСЕХ агентов; системный промпт dmp-агента «Никаких авто» проигрывал жёстким правилам.
- Решение (2026-07-10): `build_titles_messages` — dmp-guard (`if site_type == "dmp":`) с early return: отдельный B2B-промпт (лиды, заявки, цифры B2B) + явный `⛔ ЗАПРЕЩЕНО любое авто-кредитное`. Основная авто-ветка не тронута.

#### CT0000_BRAND_HALLUCINATION — brand="" → LLM выдумывала конкретную марку
- Симптом: ct0000 (Общее, brand="") генерировал «Новые Peugeot в наличии», «BAIC в наличии» (5 случаев: scherbakova/karavaev/gordeeva/salamahin).
- Root-cause: `_fanout_head` для `brand==""` оставлял `brand_block = ""` — LLM ничем не ограничена выдумывать марку.
- Решение (2026-07-10): `_fanout_head` — `elif site_type != "dmp":` добавляет `brand_block = "⛔ ЗАПРЕЩЕНО упоминать конкретную марку (Peugeot, BAIC, Chery...) — пиши обобщённо: «авто», «автомобили»"`. Для brand!="" поведение не изменилось.

#### KVIZ_BU_LEAK — Квиз использовал б/у лексику
- Симптом: для `site_type=="Квиз"` заголовки/тексты содержали «б/у», «с пробегом». Квиз — только новые авто.
- Root-cause: в `build_titles_messages` и `build_texts_messages` не было site_type-specific запрета б/у.
- Решение (2026-07-10): добавлен `_kviz_extra_titles/texts` — `if site_type == "Квиз":` вставляет `⛔ КВИЗ = только НОВЫЕ авто: ЗАПРЕЩЕНО «б/у», «с пробегом»...` в промпт заголовков И текстов. «С пробегом»/«Мульти+БУ» запрет НЕ получают.

#### TITLES_SHORT_BUDGET — короткие заголовки/тексты (свободные символы = дефект)
- Симптом: заголовки 36 симв при цели 50–56, тексты без заполнения до 75–81.
- Root-cause: промпт заголовков имел "45–47 = слабо, брак", но не "менее 48 = дефект" явно; TITLE2 не имел нижней цели; промпт текстов не указывал "целься в 75–81".
- Решение (2026-07-10):
  - `build_titles_messages`: ЗАГОЛОВКИ — "целься в 50–56, менее 48 = дефект, свободные символы = ошибка, добавь УТП/деталь"; TITLE2 — "целься в 26–30 симв; короче 22 = слабо".
  - `build_texts_messages`: "целься в 75–81; менее 70 = слабо, брак; используй все доступные символы — добавь УТП/деталь, не воду".
  - Константы используются через `TITLE_MAX`/`TITLE2_MAX`/`TEXT_MAX` (не хардкод).

- Верификация: py_compile OK; pyflakes 0 новых undefined; мини-тест PASS:
  - dmp: нет `АВТОМОБИЛИ`/`кредитный угол`, есть `B2B`/`ЗАПРЕЩЕНО любое авто-кредитное`/`лидов`
  - ct0000 brand="": есть запрет марки; для brand="Chery" — нет запрета, есть 🎯 БРЕНД
  - Квиз: `б/у`+`КВИЗ` в заголовках И текстах; «С пробегом» — без КВИЗ-запрета
  - Fill-budget: `26–30` и `используй все доступные символы` в titles; `75–81` и `менее 70 символов` в texts
  - Авто-слепки (pavlov, Монобренд, BAIC): авто-рамка цела
- Чинит ли delayed content_repair уже созданных РК: НЕТ. LLM-промпт применяется только при генерации (новые прогоны).
- Статус: 🟡 код на Mac (py_compile OK). НЕ деплоено.

### CONTENT_EDITOR_SITELINK_REORDER — новая фича: позиционная перестановка порядка быстрых ссылок (2026-07-10)
- Симптом/задача: не ошибка, а НОВЫЙ путь записи (вариант A, drag-and-drop). Массовая перестановка
  ПОРЯДКА быстрых ссылок сразу во всех кампаниях выбранных аккаунтов по позициям (permutation по индексам).
- Где: `routes_content_editor.py` — `_validate_permutation` (биекция 0..N-1, N≥2, не тождество),
  `_reorder_sitelinks` (ядро: применяет perm к КАЖДОМУ набору content["sitelinks"], per-set report
  applied/skipped/error), ветка `typ=="sitelink_reorder"` в `_do_replace` (perm лежит JSON в new_text),
  `_SITELINK_JOB_TYPES` (executor грузит campaign-level наборы для reorder), эндпоинт
  `/api/content-editor/sitelinks/reorder_async` (та же очередь content_jobs, type='sitelink_reorder',
  mode='reorder', old_text=new_text=json(perm)). Frontend `templates/direct/content_editor.html`: кнопка
  «↕️ Порядок быстрых ссылок» (только раздел sitelinks), панель `ce-reorder-panel` (drag-and-drop чипы
  позиций, селектор длины N = самая частая длина наборов), клиентское превью было→стало per-set,
  `ceReorder*` JS. Job-note для reorder показывает «переставлено наборов X, пропущено Y».
- Пути записи по типам (переиспользованы примитивы, ничего не дублировано):
  - **UAC (tp6/7)** → `_uac_patch_campaign_texts(client, cid, "sitelinks", reordered)`. Основную ссылку
    (href) НЕ трогаем. Перечитываем деталь и переставляем РЕАЛЬНЫЙ текущий массив (byte-safe), read-back.
  - **campaign-level (inheritableSitelinkSet)** → `add_sitelink_set(reordered)` + `set_campaign_sitelink_set`.
  - **ad-level TextAd/DynamicTextAd** → `add_sitelink_set` + `_v5_rebind_ads_sitelink_set`.
  - **ad-level ResponsiveAd** → честный skip «не поддерживается (хрупкость Grid)» + отчёт (не тихий успех).
  - Наборы короче перестановки (len(items) < N) → skip с явным отчётом (не режем молча).
- Возврат-безопасность: swap — инволюция; для RK дедуп `add_sitelink_set` при идентичном содержимом
  возвращает ИСХОДНЫЙ set_id (бесплатный откат); для UAC исходный порядок в отчёте (orig_order).
- Верификация (live, porg-psm5h7q6, всё State=OFF/DRAFT, swap перв.двух [1,0,2,3,4,5,6,7], возврат):
  · UAC 712694743: order swap → read-back новый порядок → revert → **byte-for-byte идентично**.
    **Основная ссылка href `https://autos-kemerovo.site/auto` — UNCHANGED** через swap и revert.
  · campaign-level set 1492751576 (камп 712694813): swap → новый набор 1492769958, камп перепривязан →
    revert → **Grid дедуп восстановил ИСХОДНЫЙ set_id 1492751576, order идентичен**.
  · ad-level ResponsiveAd set 1492662343: **skipped «не поддерживается» (responsive_skipped=2),
    replaced=0, набор НЕ тронут** (порядок unchanged после попытки).
  МАССОВАЯ запись НЕ запускалась (инициирует Семён из UI).
- ⚠️ КВИРК (не блокер, документирую): для **UAC товарки tp7** partial-PATCH `{sitelinks}` отвергается →
  `_uac_patch_campaign_texts` шлёт FULL payload → UAC на save РЕОРДЕРИТ `device_types` и `ad_group_briefs`
  (МЕМБЕРШИП идентичен: phone/desktop/tablet все на месте — меняется только ПОРЯДОК списка, семантически
  инертно; main link href НЕ трогается). Это ПРЕД-СУЩЕСТВУЮЩЕЕ поведение общего UAC-write-пути (те же
  href/title замены), НЕ регресс reorder. Партиал работает для не-товарочных UAC — там churn'а нет.
- Деплой: рестарт ОБОИХ (direct-content + direct-content-worker, оба active), HTTP 302, journal чист.
  py_compile OK, pyflakes 0 undefined, node --check OK.
- Статус: ✅ подтверждено живым прогоном 2026-07-10 (UAC + campaign-level + ad-level ResponsiveAd skip,
  read-back + откат). НЕ помогло ранее: — (новый путь).
- **Code-review фиксы 2026-07-10 (5 находок, все ✅ live-verified porg-psm5h7q6 OFF, возврат byte-for-byte):**
  1. 🟠 **Мультиаккаунт reorder применял непросмотренную перестановку.** `ceReorderApply` слал perm на
     ВСЕ `ceTargetLogins()`, но превью/`RO_N` строятся только по загруженному `CE.login` (`CE.content` = 1 логин).
     reorder ПОЗИЦИОННЫЙ → на непросмотренных акках с иным составом наборов слепая перестановка на живых
     объявлениях. Фикс (`content_editor.html` `ceReorderApply`): `targetLogins = [CE.login]` + гард на `CE.login`
     + текст подтверждения «только загруженный аккаунт». Выбран вариант «ограничить одним акком» (не warning-мультиакк):
     превью привязано к одному логину, а позиционный swap на несовпадающих наборах семантически неоднозначен.
  2. 🟡 **UAC read-back ложный «не подтвердил» при одинаковых title.** `_reorder_sitelinks` сверял порядок
     по списку TITLE → swap ссылок с одинаковым title/разным href помечался error при реально применённой
     перестановке. Фикс: сверка read-back И пре-чек «изменилось ли» по ПОЛНОМУ кортежу `_sl_tuple` =
     (title,href,description). Live-модель: OLD title-only `after==cur -> True` (ложь), NEW tuple `-> False` (верно).
  3. 🟡 **`replaced` смешивал единицы** (кампании+UAC-наборы+объявления в одно число). Фикс: основная метрика
     `replaced == applied_sets` (наборы), детализация отдельными полями `campaigns_touched`/`ads_touched`/`uac_sets`.
     UI job-note уже показывал `applied_sets`/`skipped_sets`. Live: UAC → uac_sets=1; campaign → campaigns_touched=1.
  4. 🔵 **sync `ce_replace` не отклонял substring для не-AD полей** (в отличие от `/preview` и `/replace_async`).
     Фикс: тот же гард 400 «массовая замена фрагмента только для заголовков и текстов».
  5. 🔵 **Reorder пересобирает набор из снимка (title/href/description).** ФАКТ: элемент набора состоит РОВНО из
     этих трёх полей — v5 `sitelinks.get` (Title/Href/Description), Grid `get_sitelink_sets` (title/description/href)
     и запись `add_sitelink_set` (title/href/description) оперируют той же тройкой; per-item `id` назначается
     сервером и на создании не пересылается. Полей не теряется → находка снята комментарием (несущественна).
  - Верификация: py_compile OK, pyflakes 0, node --check OK. Рестарт обоих сервисов active. Live TEST1..4 на
    porg-psm5h7q6: UAC swap→revert byte-for-byte (main-link scalar поля unchanged), campaign swap→revert Grid-дедуп
    к 1492751576, ad-level ResponsiveAd honest skip (responsive_skipped=2, набор не тронут).

### CONTENT_EDITOR_FRAGMENT_STRAY_WHITESPACE — ведущий/хвостовой пробел во фрагменте бьёт заголовок (2026-07-10)
- Симптом: в «Массовой замене фрагмента» (mode=substring) пользователь случайно оставил ведущий
  ПРОБЕЛ в поле «Стало — фрагмент». Бренд был слит с фрагментом (`Jetour2026 г в Кемерово…`), а
  превью/замена выдавали `Jetour 2026 г…` — лишний пробел между брендом и фрагментом.
- Где: `routes_content_editor.py` (preview `ce_preview`, replace `ce_replace`/`ce_replace_async`,
  worker через `make_job_executor`→`_do_replace`→`_match_targets`/`_replace_adaptive_ad_texts`) +
  frontend `templates/direct/content_editor.html` (`ceFragPreview`/guard).
- Root-cause: для обычного пробела `str.strip()`/JS `.trim()` уже чистили — этот кейс работал. Но
  НЕвидимые/zero-width символы (BOM U+FEFF, ZWSP U+200B, ZWNJ/ZWJ, word-joiner U+2060) `.strip()`
  НЕ убирает (`'﻿'.isspace()==False`), а JS `\s` не покрывает U+200B..U+200D/2060. Такой
  стрей-символ подставлялся буквально между брендом и фрагментом. Гард длины `len(new)>len(old)`
  тоже сбивался (символ увеличивал длину).
- Решение (2026-07-10): добавлен `_frag_trim()` (`.strip()` + strip невидимого набора
  `_FRAG_INVISIBLE` + повторный `.strip()`) и JS-двойник `ceFragTrim()`. Заменил ВСЕ нормализации
  `old_text`/`new_text` и все length-гарды (preview, replace, replace_async, `_do_replace`) на
  frag-trim. Внутренние пробелы не трогаются (strip только по краям). Рестарт ОБОИХ сервисов:
  `direct-content` + `direct-content-worker`.
- Верификация (LXC 101, реальный `_match_targets`): plain space / NBSP / BOM / ZWSP / word-joiner /
  trailing ZW → все дают чистый `Jetour2026 г…` (before-fix BOM/ZW вставляли символ между
  Jetour и 2026). Exact-режим цел (hit=1 / non-match=0). `_frag_trim('  a b  c ZW')`==`'a b  c'`.
- Статус: ✅ подтверждено локальным прогоном на LXC 101 2026-07-10 (оба сервиса active). Живой
  UI-прогон по аккаунту не делал (правило: без массовой записи по живым аккаунтам).
- Грабля: это ЛОГИКА → недостаточно рестарта одного сервиса, надо оба (`direct-content` для
  превью/эндпоинтов + `direct-content-worker` для реальной записи), иначе превью и запись
  разъедутся.

### CONTENT_EDITOR_SITELINK_HREF_REPLACE — Фаза 3b: смена Href/URL быстрой ссылки, приоритет UAC (2026-07-10)
- Симптом/задача: не ошибка, а НОВЫЙ путь записи. Редактор быстрых ссылок раньше менял ТОЛЬКО
  заголовок/описание (`sitelink_title`/`sitelink_description`). Сам URL (Href) элемента быстрой
  ссылки не редактировался — особенно для UAC (tp6/tp7 = Мастер/Товарка), где Семён отдельно просил.
- Где: `routes_content_editor.py` — новый тип `sitelink_href` (`_SITELINK_TYPES`/`_SITELINK_FIELD`),
  UAC-загрузка сайтлинков в `_load_account` блок 3b (`uac_sitelinks_out`, `source="uac"`,
  `set_id="uac:<cid>"`), `_match_targets` (ветвление uac/grid по `source`), новый
  `_replace_uac_sitelinks` (PATCH sitelinks + read-back), `_do_replace` sitelink-ветка (split
  uac→PATCH / grid→set-rebind), `_replace_sitelink_text_grid` + `_confirm_ads_sitelink_text`
  обобщены на field=href. Frontend `content_editor.html`: поле «Стало: ссылка/URL» в edit-боксе
  быстрой ссылки, задача `sitelink_href` через ту же очередь `content_jobs`/`replace_async`.
- Путь записи по типам (подтверждён живьём):
  - **UAC (tp6/7): cookie-PATCH `/web-api/uac/campaign/{id}` по полю `sitelinks`** (`_uac_patch_campaign_texts`,
    field_key="sitelinks", `sitelinks` уже в `_UAC_PATCH_FULL_KEYS`). Структура элемента —
    `{title, href, description}` (проверено GET detail на porg-psm5h7q6). Матч по точному значению
    поля; в UAC href часто ОДИН общий → смена меняет посадочную у всех совпавших элементов кампании.
    v5 UAC-кампании не отдаёт → ids берутся из `uac_detail_client.client.list_campaigns()` (уже было).
  - **Обычные РК (tp1-5, campaign-level inheritableSitelinkSet): Grid `add_sitelink_set`(новый набор с
    новым href) → `set_campaign_sitelink_set`(перепривязка)** — та же campaign-level машинерия, что и
    для title/desc, просто меняем поле href в items. Требует куку управляющего агентства (victorylotsofads1
    для porg-*, см. GRID_COOKIE_SUBACCOUNT_404).
  - Ad-level ResponsiveAd href — fallback через Grid `find_and_replace_text` target `SITELINK_HREF`
    (`linkReplacementMode:"FULL"`) — ФЛАКОВ (может вернуть successCount без применения, «не подтвердилась
    у N» — как и для title/desc на GdTextAd). Read-back честно репортит; данные не портятся. Основной
    (campaign-level) путь стабилен.
- Верификация (live, porg-psm5h7q6, ЧЕРНОВИКИ State=OFF/DRAFT):
  - UAC (712694741+712694743): read href `/auto` → `_do_replace(sitelink_href, /auto → /spike-test-sl-href)`
    `replaced=16 errors=[]` → read-back NEW у всех 16 → REVERT `replaced=16 errors=[]` → read-back
    восстановлено `/auto` байт-в-байт.
  - Grid campaign-level (set 1492662511, 4 кампании): href `https://autos-kemerovo.site` → `/grid-spike-test-href`
    → новый набор 1492748296, кампании перепривязаны, read-back NEW → REVERT → кампании вернулись на
    исходный 1492662511 с `https://autos-kemerovo.site`. (Ad-level ResponsiveAd в матче — 2+8 «не
    подтвердилась», см. выше; не регресс.)
  - МАССОВАЯ запись НЕ запускалась (её инициирует Семён из UI). Живые (State=ON) объявления после
    смены href уходят на модерацию — UI предупреждает в hint.
- Деплой: рестарт ОБОИХ (`direct-content` + `direct-content-worker`, правка логики воркера), оба active,
  HTTP 302, journal чист. py_compile OK, pyflakes 0, node --check OK.
- Статус: ✅ подтверждено живым прогоном 2026-07-10 (UAC И Grid campaign-level, read-back + откат).
- НЕ помогло ранее: — (новый путь). ⚠️ Ad-level ResponsiveAd href через find_and_replace SITELINK_HREF
  ненадёжен (нужны sitelinkOrderNumsToUpdateHref или set-rebind) — сейчас честный fail-report, не тихая порча.

### CONTENT_EDITOR_AD_HREF_REPLACE — Фаза 3 «Смена ссылки»: путь записи Href (2026-07-10)
- Симптом: не ошибка, а НОВЫЙ путь записи — фиксирую риски. Массовая смена посадочной ссылки (Href)
  объявлений по выбранному пути через очередь content_jobs (`type='ad_href'`, `mode='link'`).
- Где: `routes_content_editor.py` — `_replace_ad_href` (сборка по подтипу + ads.update + read-back),
  `_do_replace` (ветка `typ=='ad_href'` ПЕРЕД mode-логикой), эндпоинт `/api/content-editor/links/replace_async`,
  исполнитель тот же `make_job_executor→_do_replace`. UI `content_editor.html:ceLinksApply`.
- Root-cause/механика по типам: **ResponsiveAd.Href — ТОЛЬКО v501 `ads.update {Id,ResponsiveAd:{Href}}`**
  (v5 отвергает весь тип — Code 3500 «используйте v501», спайк подтвердил живьём). **TextAd.Href — v5
  `ads.update {Id,TextAd:{Href}}` ок.** DynamicTextAd/фид/Shopping — Href нет → в `content['links']`
  отсутствуют → пропускаются; UAC (tp6/7) основную посадочную сменить нельзя → в skipped. Новый Href =
  тот же scheme+host+fragment исходного + новый path/query (`_href_with_new_path`, urlsplit/urlunsplit —
  НЕ str.replace, host НЕ меняем). Идемпотентно: матч по old_path, уже-изменённые не совпадут.
- РИСК: живые (State=ON) объявления после смены Href уходят на ПЕРЕ-МОДЕРАЦИЮ Яндекса — UI предупреждает
  через `ceConfirm` с числом живых. Массовую запись инициирует Семён из UI, не код.
- Решение: реализовано + read-back-подтверждение (v5 GET Href работает для ОБОИХ типов). Дедуп ad_ids,
  чанки по 100, per-account задачи в очередь, дневной лимит `CE_DAILY_JOB_CAP`.
- Статус: ✅ контролируемый тест на ЧЕРНОВИКЕ (porg-psm5h7q6, ad 1915186853212398223 State=OFF,
  ResponsiveAd/v501): read→сменил Href→read-back `/auto/baic/spike-test-href`→REVERT→read-back
  `/auto/baic` байт-в-байт, `replaced=1 confirmed=1 errors=[]`. TextAd/v5-путь live НЕ тестирован
  (в аккаунте только ResponsiveAd) — код зеркалит спайк-факт. Массовый прогон НЕ запускался (ждёт Семёна).
- НЕ помогло ранее: — (новый путь). ⚠️ НЕ слать ResponsiveAd.Href в v5 (Code 3500). НЕ str.replace хоста.

### CONTENT_EDITOR_SITELINK_CAMPAIGN_LEVEL_INVISIBLE — смена заголовка/описания быстрой ссылки не работала для campaign-level наборов (2026-07-10)
- Симптом (редактор контента `/direct/automation/content`, porg-psm5h7q6): job «быстрая ссылка»
  → «заменён 1/1, ошибок 80»; job «описание ссылки» → «замена текста ссылки не подтвердилась у 8
  объявлений — Grid не применил изменение». Часть наборов быстрых ссылок вообще не видна в редакторе.
- Где: **`routes_content_editor.py:_load_account`** (сбор наборов). Запись — `_replace_sitelink_text_grid`
  (уже умела campaign-level через `grid_finalize.set_campaign_sitelink_set`, но её никто не кормил
  campaign-usages).
- Root-cause (подтверждён живьём на porg-psm5h7q6, 12 кампаний): быстрые ссылки в ЕПК бывают на ДВУХ
  уровнях — КАМПАНИЯ (`inheritableSitelinkSet`) и ОБЪЯВЛЕНИЕ (`SitelinkSetId`). `_load_account` собирал
  наборы ТОЛЬКО из `SitelinkSetId` объявлений. Диагностика показала: **v5 ads.get НЕ отдаёт
  унаследованный campaign-level набор у объявлений** — у наследующих объявлений `SitelinkSetId` ПУСТ
  (`ads_carrying_inh=0` во ВСЕХ 12 кампаниях). Из 3 campaign-level наборов два (`1492625682` — 7 кампаний,
  `1492706877` — 1) были полностью НЕВИДИМЫ редактору; третий (`1492662511`) виден лишь из-за случайного
  ad-override в чужой кампании. Замена целилась в пустой `ad_ids` → Grid ничего не применял, а ad-level
  find/replace по наследующим объявлениям давал «не подтвердилась у N».
- Решение (2026-07-10, `_load_account`): после чтения объявлений читаем набор КАЖДОЙ кампании через
  Grid `_read_unified_campaign_update_payloads` → `inheritableSitelinkSet.sitelinkSetId`; добавляем
  campaign-usage (`adgroup_id=0`) в `sitelink_usages[set_id]` и строим `campaign_ids_by_set`. За счёт
  этого set-id попадает в `sitelink_set_ids` (контент читается тем же v5 `sitelinks.get` — проверено:
  отдаёт все 3 набора с полным содержимым) и в `sitelinks_out` появляется `level=campaign` +
  `campaign_ids`. Дальше уже существующий `_replace_sitelink_text_grid` идёт campaign-level путём:
  `add_sitelink_set` (новый набор) → `set_campaign_sitelink_set` (перепривязка кампаний). Grid-чтение
  обёрнуто в try/except (`_grid_sitelink_error`), как callout-обогащение — не ломает /load.
- Ad-level НЕ сломан: для ad-level наборов campaign-usages нет → в replace гейт
  `current_set_by_campaign.get(cid) == source_set_id` фильтрует campaign_ids в [] (у кампании
  inheritable-набор ДРУГОЙ) → идёт ad-level find/replace как раньше. Чистый campaign-level набор
  (0 объявлений его несут) → `_ads_by_set` пуст → НЕТ ложного ad-level find/replace → нет «80 ошибок».
- Требует куку управляющего агентства для саб-аккаунтов (`victorylotsofads1` для porg-*) —
  `pick_working_cookie` уже приоритизирует управляющее агентство (см. GRID_COOKIE_SUBACCOUNT_404).
- Верификация (live, porg-psm5h7q6, набор `1492706877`, кампания `712694813`):
  - LOAD: теперь 3 campaign-level набора видны (`level=campaign`, campaign_ids заполнены);
    ранее невидимые `1492625682`/`1492706877` присутствуют.
  - WRITE title: `Дарим КАСКО на 1 год.` → `Дарим КАСКО на 1 год` → `replaced=1, errors=[]`,
    кампания перепривязана на новый набор, read-back: new present / old absent.
  - WRITE description: `…при покупке автомобиля` → `…при покупке нового авто` → `replaced=1, errors=[]`,
    read-back подтвердил.
  - Оба изменения ОТКАЧЕНЫ обратным replace — контент набора восстановлен байт-в-байт (Grid при
    идентичном содержимом переиспользовал исходный set-id, без orphan-набора). Никаких «80 ошибок».
- Деплой: рестарт ОБОИХ — `direct-content.service` + `direct-content-worker.service` (правка логики,
  worker обязателен, см. CONTENT_EDITOR_TWO_SERVICES_WORKER_STALE). Оба `active`, HTTP 302, journal чист.
- Статус: ✅ подтверждено живым прогоном 2026-07-10 (title И description на campaign-level наборе,
  read-back + откат; ad-level путь логически не тронут).
- НЕ помогло ранее: `_replace_sitelink_text_grid` уже содержал campaign-level ветку
  (`set_campaign_sitelink_set`), но `_load_account` не собирал campaign-level наборы → ветка получала
  пустые usages/ad_ids и не срабатывала. Корень был в ЧТЕНИИ (discovery), а не в записи.

### CONTENT_EDITOR_TWO_SERVICES_WORKER_STALE — правку логики content-editor надо катить на ОБА сервиса (2026-07-10)

**Симптом.** Задание массовой замены фрагмента `mode=substring` (заголовок) упало с
«объявление с таким текстом не найдено», хотя фрагмент в объявлениях есть.

**Корень.** Редактор контента `/direct/automation/content` обслуживают ДВА systemd-сервиса:
- `direct-content.service` (flask, порт **5021**) — отдаёт UI/шаблон/preview, роуты `routes_content_editor.py`;
- `direct-content-worker.service` — исполнитель очереди `direct_automation.content_jobs`, реально применяет замены.

При правке ЛОГИКИ в `direct/routes_content_editor.py` (напр. новый режим `mode=substring` для замены
фрагмента) рестартнули только `direct-content` → воркер продолжил крутить СТАРЫЙ код: он игнорировал
новый `mode`, падал в exact-match целого заголовка (= фрагменту не равен) → «текст не найден».
Инцидент 2026-07-10: заголовок-задание `mode=substring` упало именно так, пока воркер был на коде от 2026-07-09.

**Фикс/правило.**
- Правка ЛОГИКИ (`routes_content_editor.py`, воркер) → рестарт **ОБОИХ**:
  `direct-content.service` И `direct-content-worker.service`.
- Правка только шаблона/JS (`templates/direct/content_editor.html`) → достаточно рестарта `direct-content`.
- Колонка `mode` в `content_jobs` добавляется миграцией `ensure_jobs_table()` при старте flask.
- Рестарт (прямой ssh lxc101 может не отвечать): `ssh proxmox-ts "pct exec 101 -- systemctl restart <unit>"`.

**Статус.** ✅ причина/правило зафиксированы. Правка модала (только шаблон/JS) 2026-07-10 —
рестарт только `direct-content`, смоук HTTP 302, journal чист.

### CONTENT_EDITOR_WORKER_STALE_TEXT_NORM_IMPORT — воркер держит старый `direct.text_norm` в памяти (2026-07-28)

**Симптом.** Agent Board #42 / `content_jobs.job_id=ce_a5c527188f93` (`karavaev`,
`porg-vwnkfsr6`, `type=ad_href`, `/auto/changan/cs75-plus/i/suv-5d` →
`/auto/changan/cs75-plus/iv/suv-5d`) упала:
`cannot import name 'mentions_banned_content' from 'direct.text_norm'`.

**Корень.** `direct-content-worker.service` был запущен с 2026-07-24 и держал в `sys.modules`
старую версию `direct.text_norm`; на диске 2026-07-28 функции уже были, локальный импорт проходил.
Поздний импорт sibling-модуля (`text_gen`/`ai_agents`) обращался к cached-модулю без новых экспортов.

**Фикс/правило.**
- `content_worker.py`: перед выполнением job добавлен `_ensure_text_norm_exports()` — проверяет
  `mentions_banned_content` и `strip_banned_content`, при stale-модуле делает `importlib.reload(text_norm)`,
  при реальном отсутствии экспортов падает понятной ошибкой.
- После изменений общей логики/санитайзеров всё равно перезапускать `direct-content-worker.service`
  (см. `CONTENT_EDITOR_TWO_SERVICES_WORKER_STALE`); guard — бэкстоп для долгоживущего процесса.

**Статус.** ✅ 2026-07-28: `py_compile` OK, synthetic reload-smoke OK, worker restarted. Исходная job
переисполнена: `done`, `replaced=4`, `confirmed=4`, `errors=[]`. Live `_load_account`:
active old path = 0, new path = 4; прямой v5 по всем 23 кампаниям увидел старый path только в
`ARCHIVED` объявлениях (архив не восстанавливали и не правили).

**Повтор #43.** `ce_ee855e96b5e9` (`karavaev`, `porg-jxv3b5dm`, `ad_href`) была создана тем же
stale-воркером до фикса и лежала terminal `error` с тем же ImportError. Код на диске уже имел export
и guard, локальный импорт `mentions_banned_content`/`strip_banned_content` проходил. Live v5:
старый path был в 12 `TextAd` неархивной кампании `711066164` (`SUSPENDED`, объявления `OFF`),
архивных целей по path не было. Операция добита штатным `_replace_ad_href`: `replaced=12`,
`confirmed=12`, `errors=[]`; read-back: old non-archived = 0, new non-archived = 12. Job обновлена
в `direct_automation.content_jobs` как `done`; архивные кампании не восстанавливали.

**Повтор #44 / дополнительный hardening.** `ce_3c594f992148` (`karavaev`, `porg-wpjfppa6`,
`ad_href`) была создана тем же stale-воркером PID `227400` до рестарта и лежала terminal `error`
с тем же ImportError. Дополнительно усилены top-level импорты `text_gen.py` и `ai_agents.py`: перед
привязкой `mentions_banned_content`/`strip_banned_content` они reload-ят cached `direct.text_norm`,
если экспортов нет. Проверено `py_compile`, synthetic stale-reload smoke для обоих модулей и pytest
`direct/tests/test_create_auto_regressions.py` (66 passed). Worker перезапущен, job переисполнена:
`done`, `replaced=24`, `confirmed=24`, `errors=[]`; прямой v5 read-back по 25 кампаниям/7581 ads:
old path = 0, new path = 24. Архивные кампании не восстанавливали.

**Повтор #45 / ad_href без UAC-cookie.** `ce_5a3c1400405e` (`karavaev`, `direct778`, `ad_href`)
была создана тем же stale-воркером PID `227400` до рестарта и лежала terminal `error` с тем же
ImportError. Кодовый импорт на диске проходил; live v5 перед записью: 31 неархивная кампания,
21430 ads, старый path в 36 `TextAd`, новый path = 0. Операция добита через `_replace_ad_href`
по v5-only `links`-снимку: `done`, `replaced=36`, `confirmed=36`, `errors=[]`; точечный live
`ads.get` по 36 изменённым ad_id: old path = 0, new path = 36. Доп. hardening:
`content_editor_helpers._load_account` получил `include_uac_campaigns`; worker для `ad_href` и
endpoint `/links` передают `False`, потому что UAC Href этим обработчиком не редактируется, а
cookie-enrichment на `direct778` подвисал. Проверено `py_compile`, synthetic smoke
`include_uac_campaigns=False` (`uac_calls=0`), restart `direct-content`/`direct-content-worker`.

### BRAND_ISOLATED_NOT_INTEGRATED — «Belgee.» как рублёный первый элемент, без интеграции в фразу (Д1, 2026-07-10)
- Симптом (Семён, прогон на аккаунтах Belgee): заголовки «Belgee. Авто в наличии. Кредит от 9 000 ₽/мес»,
  «Belgee. Новые автомобили. Выгода до 45%» — бренд как изолированное слово + точка + рублёный УТП.
  Выглядит механически, нечитаемо, не как живой заголовок. brand-first ≠ brand-isolated.
- Где: **`create_set_assets.py:_upgrade_credit_titles`** (строки ~312-324, variants для `brand_real=True`).
  5 вариантов вида `f"{brand}. {отдельный УТП}"` — все BAD. Также промпт `ai_agents.build_titles_messages`
  не запрещал этот паттерн явно.
- Root-cause: фикс `TITLES_ALL_FINANCIAL_NO_AUTO_CONTEXT` (Д1 2026-07-10) добавил авто-контекст через
  позиции 1 и 4, но использовал шаблон «{brand}. Авто в наличии.» — бренд стал изолированным первым
  словом перед точкой. Аналогично позиции 6, 7, 9 (резервы) — все вида «{brand}. {УТП}».
  `_brand_first_reorder` фиксирует порядок СЕГМЕНТОВ, но не видит «бренд уже первый» как проблему.
- Решение (2026-07-10):
  - `create_set_assets.py:_upgrade_credit_titles` (brand_real=True): все 5 изолированных → интегрированные:
    поз.1 `f"{brand}. Авто в наличии. Кредит…"` → `f"{brand} в наличии. Кредит от 9 000 ₽/мес"`;
    поз.4 `f"{brand}. Новые автомобили. Выгода до 45%"` → `f"Новый {brand}. Выгода до 45%"`;
    поз.6 `f"{brand}. Трейд-ин до 150% цены авто"` → `f"{brand} трейд-ин. До 150% цены авто"`;
    поз.7 `f"{brand}. КАСКО на 1 год бесплатно"` → `f"{brand} и КАСКО на 1 год бесплатно"`;
    поз.9 `f"{brand}. Госпрограмма 2026 и кредит"` → `f"{brand} по госпрограмме 2026. Кредит"`.
  - `ai_agents.py:build_titles_messages` (~строка 2185+): добавлен явный запрет
    «⛔ ЗАПРЕЩЕНО `{Бренд}.` как изолированная первая фраза» с примерами BAD/НОРМА.
  - DOD §2.1: уточнение «бренд в ЖИВОЙ ФРАЗЕ, не изолирован».
  - Smoke-test: `_upgrade_credit_titles(seq_weak, cap=7)` для Belgee → 0 вариантов «Belgee.» из 7.
- Чинит ли delayed content_repair уже созданных РК: НЕТ. `fix_brand_not_first` проверяет
  «до первой точки», не «изолированность» — уже созданные РК остаются. Фикс только для будущих прогонов.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined; smoke 0/7 изолированных). НЕ деплоено.
- НЕ помогло ранее: Д1-фикс (TITLES_ALL_FINANCIAL) — добавил авто-контекст, но ввёл изолированный бренд;
  `_brand_first_reorder` — ставит бренд в начало, но не требует интеграции в фразу.

### SLEPOK_VOICE_GENERIC_STARTERS — идентичный контент между слепками: стартовые профили без корпуса (Д3, 2026-07-10)
- Симптом (Семён): контент объявлений ОДИНАКОВ у разных директологов — нет посыла/голоса/уникальности
  конкретного слепка. Заголовки/тексты «generic кредит-шаблон» для всех.
- Где (3 корня):
  1. **`ai_agents.py:AGENT_ADS` (строки ~838-934)**: корпус реальных объявлений есть ТОЛЬКО для 5 слепков
     (pavlov/kryuchkova/scherbakova/terehov/karavaev). 6 стартовых (salamahin/gordeeva/zubakin/chepelev/
     tumashenko/kuderko) — НЕТ записи в AGENT_ADS → `ai_agents.py:937-939` НЕ добавляет ключ `ads` →
     `build_titles_messages:2133` `agent.get("ads", {}).get("titles") = []` → `ex_titles = "(адаптируй свой тон)"` →
     LLM видит НУЛЕВОЙ пример стиля → generic вывод.
  2. **`ai_agents.py:AGENTS` (строки ~243-343)**: 6 стартовых профилей имели идентичные `promo.plus` и
     совпадающие `system`-описания (salamahin = zubakin = chepelev слово-в-слово; gordeeva = tumashenko = kuderko).
     LLM не может дифференцировать → одинаковый контент.
  3. **`create_set_assets.py:_upgrade_credit_titles` (строки ~312-324)**: fallback-варианты полностью
     slepok-agnostic — используют `anchor`/`brand` но НЕ UTPs агента. Когда `_needs_credit_title_upgrade=True`,
     все слепки получают одни шаблоны.
- Root-cause (главный): для стартовых слепков данные (корпус) отсутствуют → нечего дифференцировать.
  Для реальных слепков (pavlov и др.) голос ЕСТЬ (system/ads/cross_signature) — они уже разные.
- Решение (2026-07-10, код — частичный фикс):
  - `ai_agents.py:AGENTS` (строки ~243-343): 6 стартовых профилей дифференцированы: каждый получил
    РАЗНЫЕ `promo.examples` (посылы акций) и РАЗЛИЧНЫЙ `system`-акцент (salamahin=наличие+быстрота,
    gordeeva=трейд-ин+б/у, zubakin=кредит/платёж, chepelev=квиз+онлайн-заявка, tumashenko=господдержка,
    kuderko=мультибренд+б/у). `promo.plus` также дифференцированы.
  - `ai_agents.py:_fanout_head` (строка ~2107): добавлено `promo_hint` — в шапку промпта теперь идут
    «Характерные посылы акций этого агента» из `promo.examples` → LLM получает дополнительный сигнал
    уникальности для каждого слепка.
  - **ПОЛНЫЙ фикс (2026-07-10, R2-9 Д3):** `ai_agents.py:AGENT_ADS` (строки 943–1092): добавлены
    реальные корпуса для всех 6 стартовых слепков из живых аккаунтов (харвестер):
    salamahin(10t/5tx/8sl), gordeeva(10t/6tx/8sl), zubakin(10t/5tx/7sl),
    chepelev(10t/6tx/8sl), tumashenko(8t/6tx/8sl), kuderko(10t/2tx/8sl).
    После for-loop (~строка 1093) все 6 инжектированы в AGENTS[slepok]["ads"].
    LLM теперь видит реальный голос (ex_titles/ex_texts/ex_sitelinks) вместо "(адаптируй свой тон)".
    Формат: кортежи ("title","desc") — идентично pavlov/kryuchkova. py_compile OK; pyflakes 0 новых.
- Чинит ли delayed content_repair уже созданных РК: НЕТ. LLM-голос применяется только при генерации
  (новые прогоны). Фикс только для будущих.
- Статус: ✅ полный данных-фикс на Mac (py_compile OK; AGENTS[slepok]["ads"] != {} для всех 6;
  pyflakes 2 pre-existing f-string warnings строки 2310/2388 — не мои). НЕ деплоено.
- НЕ помогло ранее: —

### TP7_SHOPPING_FEED_FILTER_MINUS_MARKS_DROPPED — UAC-условие без `values` → минус-марки товарки пропадали живьём (2026-07-10)
- Симптом: в tp7 (Товарка, ShoppingAd) НЕ добавились глобальные минус-марки (исключение чужих брендов
  через фид-фильтры). По DOD §3.7 «Товарка (ShoppingAd, feed_filters=it_ff) → глоб. минус-марки ВСЕГДА»,
  но на живых РК их нет; в кабинете feed_filters товарки пусты.
- Где: **`create_set_feeds.py:_minus_marks_uac_conditions`** (:1397/1400) и позитив
  **`_tp7_product_feed_filters`** (:1507). Сборка it_ff — `create_set_master_product.py:563`
  (`it_ff = _tp7_product_feed_filters(...)`) → `spec.feed_filters` → `campaign.py:1582`
  `payload["feed_filters"]` (шлётся как есть, без трансформации).
- Root-cause: наши UAC-условия эмитили ТОЛЬКО `value` (json-строка), без ключа `values` (реальный
  массив). HAR-эталон (`direct.yandex.ru.59har.har`, PATCH `/web-api/uac/campaign/712694743` + result)
  показал: UAC хранит условие в ДВОЙНОМ формате — `values`+`value`:
  `{"field":"name","operator":"NOT_CONTAINS","values":["uaz"],"value":"[\"uaz\"]"}`. Без `values` UAC
  ИГНОРИРУЕТ условие → сохраняет `feed_filters=[{"conditions":[]}]` (в HAR товарка ровно так и пуста, а
  listings-name-фильтр с `values` — сохранился). Grid-путь (`_minus_marks_grid_conditions`,
  `NOT_CONTAINS_ALL`+`stringValue`) — ОТДЕЛЬНЫЙ, не тронут. Инъекция `_enabled_minus_marks` жива
  (blueprint:6316/6923/7947), поле резолвится верно (AUTO_RU фид 3537034 → mark_id/folder_id, HAR
  FeedOffersPreview подтвердил). НЕ путать с defect A (текстовые минус-СЛОВА tp7-автотаргета) — тот
  фикс не тронут.
- Решение (2026-07-10): в `_minus_marks_uac_conditions` (минус-марки И минус-модели) и в позитивном
  CONTAINS по модели (`_tp7_product_feed_filters`) добавлен `"values": <массив>` рядом с
  `"value": json.dumps(<массив>)`. Итог совпадает с эталоном: `value == json(values)`, оператор
  `NOT_CONTAINS`/`CONTAINS`, поле vendor/model (или mark_id/folder_id). Изолир. тест: ct0000-общая и
  брендовый ct → условия несут и `values`, и `value`; assert `json.loads(value)==values`.
- Не трогал: collectionId listings-фильтры (`:1560`, master_product:573) — проверены живьём с
  `value`-only (DOD 2026-07-07), member-of-set id, менять = риск регрессии. Grid-путь без изменений.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 НОВЫХ undefined; изолир. структурный тест PASS).
  НЕ деплоено (по указанию Семёна). live не проверено — проверит tp7-прогон: в кабинете feed_filters
  товарки несут NOT_CONTAINS по чужим маркам (conditions НЕ пусты).
- НЕ помогло ранее: прежняя правка (гейт `c_ct != "ct0000"` убран, общие товарки тоже получают it_ff)
  устранила ОТСУТСТВИЕ фильтра в сборке, но условие всё равно молча дропалось UAC из-за формата.

### SITELINK_REUSE_RACE_PARALLEL_CHANNELS — параллельные каналы держали РАЗНЫЕ наборы сайтлинков (FIX6/#4, 2026-07-10)
- Симптом (верификатор на живых РК через Grid, прогон af4bd7bd5a52): reuse сайтлинков работает ВНУТРИ
  канала, но МЕЖДУ каналами набор РАЗНЫЙ — tp1→`inheritableSitelinkSet=1492662511`, tp5→`1492625682`,
  tp2→`1492662343`. Семён требует ОДИН набор на ВЕСЬ логин (все кампании всех tp).
- Где: **`ai_content.py:_account_sitelinks_get/put`** (get и put — РАЗДЕЛЬНЫЕ локи) +
  **`create_set_orchestrator.py:759-778`** (tp1/tp2/tp5) / **`create_set_master_product.py:421-444`**
  (tp6/tp7). Кэш `_ACCOUNT_SITELINKS_CACHE` — process-global module-dict (НЕ thread-local), это верно;
  корень не в scope кэша.
- Root-cause: `DIRECT_PARALLEL_CHANNELS=1` → каналы A(units)/B(cookie) стартуют в двух потоках
  ~одновременно. Паттерн был get→(генерация своего набора)→put, НЕ атомарный: каждый канал делал
  `_account_sitelinks_get`=None (кэш пуст) → генерил СВОЙ набор → использовал его → `_account_sitelinks_put`
  ПОЗЖЕ (первый put выигрывал кэш, но остальные каналы УЖЕ взяли свой). → 3 разных набора.
- Решение (2026-07-10, FIX6/#4): атомарный **`_account_sitelinks_get_or_put(login, candidate)`** под
  ОДНИМ `_ACCOUNT_SITELINKS_CACHE_LOCK`: если эталон есть (жив по TTL) → вернуть ЕГО; иначе если
  candidate полон (≥8) → зафиксировать эталоном и вернуть. Первый канал с полным набором сеет, все
  остальные (включая поздний tp7) берут ТОТ ЖЕ. Wired в orchestrator (tp1/tp2/tp5) и master_product
  (tp6/tp7) вместо get→…→put. `_account_pay_unify` (детерминированный account-канон) применяется после
  override → финальные наборы совпадают. pct-safety per-item сохранена (эталон с «%» при %-заголовке →
  остаёмся на своём — редкое исключение).
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined; concurrency-тест: 3 гонящихся канала +
  поздний tp7 → 1 общий набор). НЕ деплоено (работает при `DIRECT_SITELINK_REUSE_ACCOUNT=1`, уже ON).
  live не проверено — проверит прогон: ВСЕ кампании логина → ОДИН `inheritableSitelinkSet`.
- НЕ помогло ранее: R2-4(c2) reuse через раздельные get/put — устранял reuse ВНУТРИ канала, но
  межканальную гонку при PARALLEL=1 НЕ ловил (не атомарно).

### SHOPPING_DEFAULT_TEXT_HARDCODED_CREDIT — FIX5 ЗАКРЫТ (подтверждено на сервере 2026-07-10)
- Верификатор (по СТАЛЕ-ERRORS_JOURNAL) сказал `create_set_feed_builders.py:709/733` «не деплоен»
  (hardcoded «…по кредиту…»). Проверка на LXC101 `/opt/scripts/home/seoadvanced/direct/
  create_set_feed_builders.py:712`: `from .create_set_assets import SHOPPING_DEFAULT_TEXT as _SDT` →
  `texts=[_SDT]` (fail-safe fallback БЕЗ кредита). «по кредиту» осталось ТОЛЬКО в комментарии («был
  hardcoded»). Commit 31e30f8 (R2-8) задеплоен. ✅ FIX5 ЗАКРЫТ — стале-запись верификатора.

### GRID_COOKIE_SUBACCOUNT_404_BLIND_VERIFIER — Grid слеп на агентских саб-аккаунтах (Task #34, 2026-07-10)
- Симптом (прогон af4bd7bd5a52, porg-psm5h7q6): верификатор/delayed content_repair НЕ поймали 7 дефектов
  R2-8 — только Семён глазом по скринам. Grid-чтение на саб-логине porg-* возвращало пусто/«No rights».
- Где: **`campaign.py:pick_working_cookie`** (перебор `DEFAULT_COOKIE_ACCOUNTS`). Grid-клиенты
  (`grid_read.GridReadClient`, `grid_finalize.GridClient`, `grid_create`) все берут куку через
  `cmc.pick_working_cookie(login)` где login = саб-логин.
- Root-cause: `fetch_cookie_glavpotok("porg-psm5h7q6")` = **404** (у главпотока НЕТ куки саб-аккаунта);
  `fetch_cookie_glavpotok("victorylotsofads1")` = **200** (кука АГЕНТСТВА, аутентифицирует Grid для
  саб-аккаунта — правило Семёна: ВСЕГДА кука агентства). `pick_working_cookie` перебирал 6 агентств БЕЗ
  приоритета: первая ЖИВАЯ агентская кука побеждала, даже если это агентство НЕ управляет данным
  саб-логином → link_info мог пройти по generic-URL, но Grid потом «No rights» → верификатор слеп.
- Решение (2026-07-10, Task #34):
  - `campaign.py`: инъектируемый резолвер `set_agency_resolver(fn)` + `_resolve_managing_agency(ulogin)`
    (без импорта blueprint — циклический). `pick_working_cookie`: управляющее агентство
    (`dict.fromkeys((_mng, *accounts))`) пробуется ПЕРВЫМ, остальные — фолбэк.
  - `blueprint.py`: `cmc.set_agency_resolver(lambda login: _resolve_agency_hint(login, ""))` (кэш БД +
    `local_gsheet_sites.agency_account`, без API-вызовов, best-effort).
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined; изолированный тест: victorylotsofads1
  пробуется первым для porg-psm5h7q6). НЕ деплоено. live не проверено — проверит прогон: Grid-чтение и
  ремонт работают на porg-* (верификатор видит созданные РК, ловит дефекты сам).
- НЕ помогло ранее: link_info-валидация в pick_working_cookie проверяла живость сессии, но НЕ
  приоритет управляющего агентства → первая живая (но чужая) кука закрывала доступ.

### UAC_SITELINKS_LT8_DESC_DEDUP_STARVE — tp7 <8 сайтлинков из-за desc-дедупа _norm_sitelinks (DEFECT 2/4, 2026-07-10)
- Симптом (прогон af4bd7bd5a52, porg-psm5h7q6): tp7 быстрые ссылки < 8 (было 6/8). Даже после R2-6
  (2 backup-филлера без «%») набор в кабинете неполный.
- Где: **`campaign.py:_norm_sitelinks`** (:1782-1806) дедупит по title И **description** (защита от
  UAC `DUPLICATE_SITELINK_DESCS`). **`create_set_master_product.py`** сборка сайтлинков дедупила по
  ПАРЕ `(title.lower, desc.lower)`.
- Root-cause: рассинхрон дедупов. Сборка допускала два сайтлинка с ОДИНАКОВЫМ description но разными
  title (разная пара → оба проходят), а `_norm_sitelinks` (desc-дедуп) один из них РЕЗАЛ → в UAC-payload
  уходило <8. R2-6 %-backup помогал против %-фильтра, но не против desc-коллизий.
- Решение (2026-07-10, DEFECT 2/4, `create_set_master_product.py` перед сборкой spec): финальный ГАРАНТ —
  дедуп по описанию (зеркало `_norm_sitelinks`: title И desc) + добор до 8 из `_GENERIC_SITELINK_FILLERS`
  (у всех 10 УНИКАЛЬНЫЕ описания) с НЕиспользованными title/desc, %-safe (при %-заголовке %-филлеры
  пропускаются, остаётся 8 без-% филлеров). Итог: ровно 8 сайтлинков с уникальными описаниями → все
  переживают `_norm_sitelinks` → в кабинете 8/8. Smoke: dup-desc + %-title → 8 uniq-desc; пустой → 8.
- Reuse на логин (DEFECT 4): механизм `ai_content._account_sitelinks_get/put` уже wired на ВСЕ tp
  (orchestrator tp1/tp2/tp5 :759-778, master_product tp6/tp7 :423-442), флаг
  `DIRECT_SITELINK_REUSE_ACCOUNT` дефолт OFF — Семён включит на деплое. Финальный desc-гарант стоит
  ПОСЛЕ reuse-override → эталон тоже нормализуется до 8 уникальных.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined; smoke 8/8 uniq-desc). НЕ деплоено. live
  не проверено — проверит tp7-прогон: `sitelinks_count=8` в кабинете.
- НЕ помогло ранее: R2-6 (2 backup-филлера без «%») решал ТОЛЬКО %-фильтр-старвейшн; desc-коллизия
  `_norm_sitelinks` оставалась.

### TP7_AUTOTARGET_RENDERS_MANUAL_AUDIENCE — глоб.минус-слова флипали tp7-автотаргет в «Настроить вручную» (DEFECT 3, 2026-07-10)
- Симптом ПОДТВЕРЖДЁН в кабинете (прогон af4bd7bd5a52, porg-psm5h7q6, tp7 камп **712694741**): имя
  «Товарная - Автотаргетинг», а блок «Аудитория» = **«Настроить вручную»**, «Интересы и поисковые
  запросы» ПУСТО, «Минус-слова: отзывы». Ожидание Семёна: tp7-автотаргет → **«Подобрать оптимальную»**,
  минус-слова НЕ нужны.
- Где: **`create_set_master_product.py:596`** (сборка `MasterCampaignSpec.minus_keywords`) →
  **`campaign.py:build_payload:1511`** (`"minus_keywords": spec.minus_keywords`).
- Root-cause: мод-резолюция КОРРЕКТНА — `_tp67_targeting_mode("Товарная - Автотаргетинг")` = autotarget
  (проверено против slepki_structure.json), payload уже слал `keywords=[]`, `audiences=[]`,
  `relevance=OPTIMAL` («Подобрать оптимальную»). НО `minus_keywords` слался ВСЕГДА
  (`list((it_minus_keywords or []) + _enabled_minus_words())` = глоб. «отзывы»). Для UAC-ТОВАРКИ
  (product) при пустых keywords/audiences ЕДИНСТВЕННЫЙ ручной сигнал = minus_keywords → блок
  «Аудитория» флипался в «Настроить вручную». Т.е. корень не в relevance/мод-резолюции, а в
  минус-словах как последнем ручном маркере.
- Решение (2026-07-10, эталон Семёна по кабинету — HAR не понадобился):
  - `create_set_master_product.py:596`: `minus_keywords=([] if (is_product and
    targeting_mode == "autotarget") else list(dict.fromkeys((it_minus_keywords or []) +
    _enabled_minus_words())))`. tp7-автотаргет → минус-слова НЕ шлём → кабинет рендерит «Подобрать
    оптимальную». Ручные режимы tp7 (keywords/audience) — минус как есть. tp6-мастер (is_product=False)
    НЕ тронут (рендерит верно).
  - Ранее (rebuild-ветка `:536`): `is_auto=(targeting_mode == "autotarget")` — консистентность имени.
- Было vs теперь (payload tp7-автотаргет): было `minus_keywords=["отзывы"]` → «Настроить вручную»;
  теперь `minus_keywords=[]` + `keywords=[]` + без `ca_retargeting_condition` + `relevance.active=True`
  categories=OPTIMAL → «Подобрать оптимальную». Верифицировано изолированным вызовом `build_payload`.
- Чинит ли delayed content_repair уже созданные РК (712694741): НЕТ (аккаунт по указанию Семёна НЕ
  трогаем). Фикс только для СЛЕДУЮЩЕГО прогона.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined; build_payload-тест: minus=[], keywords=[],
  no ca_retargeting, relevance=OPTIMAL). НЕ деплоено. live не проверено — проверит tp7-прогон: блок
  «Аудитория» = «Подобрать оптимальную», минус-слов нет.
- НЕ помогло ранее: consistency-фикс `is_auto` (имя) не менял payload → кабинет оставался «вручную»,
  пока не убрали minus_keywords для is_product+autotarget.

### TITLES_ALL_FINANCIAL_NO_AUTO_CONTEXT — все 7 заголовков про финансы, продукт (авто) не виден (Д1, 2026-07-10)
- Симптом (прогон af4bd7bd5a52, porg-psm5h7q6, tp1 Марки-КС BAIC 712686247): ВСЕ 7 заголовков —
  чисто финансовые УТП (кредит/взнос/КАСКО/одобрение/банки/выгода/трейд-ин). Ни один не сообщает
  что продаём АВТОМОБИЛИ. Пользователь не понимает продукт.
- Где: **`ai_agents.build_titles_messages`** (промпт «ОБЩАЯ РАМКА») + **`create_set_assets._upgrade_credit_titles`**
  (варианты для `brand_real=True`, все 7 позиций — только кредитные УТП).
- Root-cause: промпт требовал «В КАЖДОМ заголовке должен читаться кредитный оффер» — без исключения
  для авто-контекста. Upgrade-варианты (9 штук) тоже все финансовые. Ни промпт, ни детерминированный
  fallback не обязывали включить «авто в наличии» / «новые автомобили» в набор из 7.
- Решение (2026-07-10):
  - `ai_agents.py:build_titles_messages` — заменена «ОБЩАЯ РАМКА»: теперь «1–2 из 7 ОБЯЗАТЕЛЬНО
    сообщают ПРОДУКТ (авто в наличии / новые автомобили); остальные 5 — кредитный угол».
  - `create_set_assets._upgrade_credit_titles` (brand_real=True): позиции 1 и 4 из cap=7 заменены:
    - поз.1: `f"{brand}. Авто в наличии. Кредит от 9 000 ₽/мес"` (AUTO-CONTEXT + credit);
    - поз.4: `f"{brand}. Новые автомобили. Выгода до 45%"` (AUTO-CONTEXT + discount).
    Прежние КАСКО (поз.1) и «15 банков» (поз.4) смещены на позиции 7–8 (резерв, >cap).
    Smoke-test: 2/7 AUTO-CONTEXT, все brand-first, все с цифрой, 6 уникальных 18-симв. префиксов.
- Чинит ли delayed content_repair уже созданные РК (712686247): НЕТ. Нет аудитора/фиксера для
  «ВСЕ заголовки финансовые» в delayed-repair. Фикс только для будущих прогонов.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined). НЕ деплоено.
- ⚠️ **ДЕФЕКТ-В-ФИКСЕ:** поз.1 `f"{brand}. Авто в наличии. Кредит от 9 000 ₽/мес"` и поз.4
  `f"{brand}. Новые автомобили. Выгода до 45%"` — «{brand}.» = ИЗОЛИРОВАННЫЙ бренд (рублёный зачин).
  Семён нашёл это как Дефект 1 (следующий прогон, Belgee). Исправлено в BRAND_ISOLATED_NOT_INTEGRATED
  (2026-07-10): поз.1 → `f"{brand} в наличии. Кредит от 9 000 ₽/мес"`, поз.4 → `f"Новый {brand}. Выгода до 45%"`.
- НЕ помогло ранее: R2-6 (вариативность захода) — добавил разные 18-симв. префиксы, но все варианты
  остались финансовыми. D11 (стиль формулировок) — улучшил формулировки кредита, авто-контекст не добавил.

### SHOPPING_DEFAULT_TEXT_CREDIT_FORBIDDEN — default_text ShoppingAd содержал кредитную лексику (Д5, 2026-07-10)
- Симптом (прогон af4bd7bd5a52, tp1 Модели-КС BAIC U5 Plus): «Текст по умолчанию» =
  «Купить авто в кредит от 9 000₽/мес. Первый взнос 0 ₽. Одобрение за 30 минут» —
  в ShoppingAd/каталожных объявлениях кредитная лексика ЗАПРЕЩЕНА (Семён 2026-07-10).
- Где: **`create_set_assets.SHOPPING_DEFAULT_TEXT`** (строка 92) — константа содержала кредит/взнос/одобрение.
- Root-cause: предыдущий фикс (Fix 4, SHOPPING_DEFAULT_TEXT_UNDERFILLED, 2026-07-10) исправил
  ДЛИНУ (50→70 симв) и добавил кредитный угол — но не учёл что ShoppingAd запрещает кредитную
  лексику. «Кредитный угол обязателен» из DoD §2 относится к ResponsiveAd/TextAd, НЕ к ShoppingAd.
- Решение (2026-07-10):
  `create_set_assets.SHOPPING_DEFAULT_TEXT` изменён с
  `"Авто в кредит от 9 000 ₽/мес. Первый взнос 0 ₽. Одобрение за 30 минут."` (70 симв, кредит)
  на `"Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."` (81 симв).
  Проверено: нет кредит/взнос/платёж/одобрение; содержит авто-контекст; ровно 81 символ.
- Чинит ли delayed content_repair уже созданных РК: НЕТ. `_campaign_default_text_repair` — STUB.
  Фикс только для будущих прогонов.
- Статус: 🟡 код на Mac (py_compile OK; len=81 ≤81; no credit). НЕ деплоено.
- НЕ помогло ранее: Fix 4 (SHOPPING_DEFAULT_TEXT_UNDERFILLED) — исправил длину, но ввёл кредит.

### SHOPPING_DEFAULT_TEXT_MULTIPLE_SOURCES — разные источники default_text на разных путях (Д6, 2026-07-10)
- Симптом (прогон af4bd7bd5a52): «Текст по умолчанию» разный по кампаниям одного набора —
  cookie-путь (create_set_gallery) использовал SHOPPING_DEFAULT_TEXT; repair-путь
  (_repair_shopping_content_context) брал texts[0] из item-контента; API-путь tp5
  (_create_tp5_single) брал data.get("default_text") из direct_slepok_content или hardcoded fallback.
- Где: 3 независимых источника default_text:
  1. **`create_set_gallery.py`** — SHOPPING_DEFAULT_TEXT (константа) ✅ правильно;
  2. **`create_set_repairing.py:_repair_shopping_content_context`** — texts[0] ❌;
  3. **`create_set_feed_builders.py:_create_tp5_single`** — data.get("default_text") + hardcoded
     `"Официальный дилер. Тест-драйв и выгодные условия по кредиту. Авто в наличии."` ❌ (инфра).
- Root-cause: Fix 4 подключил константу ТОЛЬКО в create_set_gallery (cookie-путь), но repair-путь
  и API-путь tp5 остались на старых источниках.
- Решение (2026-07-10):
  - `create_set_repairing.py:_repair_shopping_content_context` (строки 169-180): заменён блок
    `body_text = _trim_clean(texts[0] ...)` на import SHOPPING_DEFAULT_TEXT с fail-safe на texts[0].
  - `create_set_feed_builders.py:_create_tp5_single` (строки 709, 733) — НЕ трогал (инфра-граница,
    `create_set_feeds*` = зона direct_neyrodirektolog). Задача главной сессии: строки 709 и 733
    заменить на `from .create_set_assets import SHOPPING_DEFAULT_TEXT` + убрать hardcoded fallback.
    Также строка 636 в `_tp5_account_data` читает из `direct_slepok_content.texts[0]` — это тоже
    нужно переключить на константу или игнорировать это поле для body_text.
- Чинит ли delayed content_repair уже созданных РК: НЕТ. Только будущие прогоны.
- Статус: 🟡 repair-путь исправлен (py_compile OK). API-путь tp5 (infra) — не деплоено, ждёт правки
  `direct_neyrodirektolog`. live не проверено.
- НЕ помогло ранее: Fix 4 — закрыл только cookie-путь.

### KEYWORD_REPAIR_NO_PACK_SILENTLY_OK — КС-группа без ключей пака засчитывалась как «ok» в keyword-repair (P0, 2026-07-10)
- Симптом (прогон 59581fdd9f9d, delayed_repair 0210a981b2b0): `keywords_repair` пишет «нет групп для
  keyword-repair (всё уже корректно/идемпотентно), skipped_groups=73» — хотя `campaign_spec_audit`
  флагует `NO_KEYWORDS_LIVE`. tp2 712665661 Модели-КС — 0 ключей; tp5 ct0032 Changan CS55 — 1 seed-ключ.
  Рассинхрон audit↔repair: одно видит проблему, другое говорит «всё ок».
- Где: **`repair_executor.py:execute_keywords_repair`** строки ~720-728 (блок `need_kw and not
  writable_kw and not need_at`).
- Root-cause: когда `need_kw=True` (группа без ключей по showConditions) + `writable_kw=[]` (пак M3
  недоступен или ct нет в паке → `kp.gather(...)={}` → `pos=[]`) + `need_at=False` (КС-кампания) —
  вся группа клалась в `results` как `ok=True, skipped="нет источника ключей (автотаргет активен)"`.
  `write_items=[]` → `failed=[]` → функция возвращала «нет групп для keyword-repair» вместо ошибки.
  Корень: одна ветка покрывала и AT-by-design (правильный skip) и КС-без-пака (должен быть failed).
- Решение (2026-07-10, repair_executor.py строки 719-737):
  - Ветка `need_kw and not writable_kw and not need_at` теперь ветвится:
    `"автотаргетинг" in (camp_name or "").lower()` → AT-by-design → `ok=True, skipped` (как раньше);
    иначе (КС-кампания без пак-данных) → `failed.append(error="нет ключей от pack для этого ct
    (pack недоступен/пуст; поисковая группа без ключей)")`.
  - Итог: честный статус — КС с пустым паком → failed → audit↔repair консистентны.
- Чинит ли delayed content_repair уже созданные РК (712665661, ct0032): ЧАСТИЧНО. Новый код
  теперь **правильно сообщает failed** вместо «всё ок», но ключи всё равно не зальются, пока
  `kp.gather(slepok, site_type, tp_code)` не вернёт данные (M3-пак). Если M3 недоступен или ct
  отсутствует в паке — ключи невозможны физически. Когда пак появится — delayed-repair на следующей
  итерации добьёт. Только для будущих прогонов: если корень в данных пака — нужна сверка
  `direct_slepki_master` (ct0032 Changan CS55 в паке psm5h7q6).
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined). НЕ деплоено. live не
  проверено — проверит следующий прогон: КС-без-пака больше не маскируется под «ok».
- НЕ помогло ранее: —

### TITLE_MONOTONE_PREFIX_ALL_BRAND_CITY — все 7 заголовков с одинаковым 18-сим. зачином (Fix 1, 2026-07-10)
- Симптом (прогон 59581fdd9f9d, tp1 Belgee 712664560): ВСЕ 7 заголовков — «Belgee в Кемерово.
  {УТП}»; первые 18 символов идентичны. Дефект вариативности захода (DOD §2.1.с).
- Где: **`create_set_assets.py:_upgrade_credit_titles`** (~строки 284-320), TOKEN-финал tp1/tp2/tp4.
  Список `variants` для `brand_real=True` содержал 9 шаблонов, ВСЕ начинались с `f"{anchor}. …"`,
  где `anchor = f"{brand} в {city}"` — т.е. «Belgee в Кемерово.» всегда первые 18+ символов.
- Root-cause: вариативность подхода R2-3 (brand-first детерминированный реордер) добавила бренд
  в начало, но НЕ добавила разнообразие начальных конструкций — все варианты были {anchor}.{УТП}.
  Городской якорь склеен с брендом в КАЖДОМ варианте → первые ~18 символов одинаковые.
- Решение (2026-07-10, create_set_assets.py строки 292-310):
  Variants смешаны: `{anchor}` (brand+город), `{brand}` (без города), `"Купить {brand} …"`,
  `"{brand} в кредит …"` — 5 уникальных 18-символьных префиксов из 7 заголовков.
  Smoke-test: «Belgee в Кемерово.», «Belgee.», «Купить Belgee», «Belgee в кредит.» + «Belgee.» —
  5 различных зачинов, все brand-first, все кредитный угол.
- Чинит ли delayed content_repair уже созданные РК (712664560): НЕТ. `_upgrade_credit_titles`
  исполняется только при TOKEN-создании. Delayed-repair может вызвать `fix_brand_not_first` (regen
  LLM), но специальной логики «дозаливки вариативности» нет — существующие РК остаются как есть.
  Фикс только для будущих прогонов.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined; smoke 5/5 уникальных
  префиксов из 7 заголовков). НЕ деплоено. live не проверено.
- НЕ помогло ранее: R2-3 — добавил brand-first реордер, но варианты остались все {anchor}-based.

### SHOPPING_DEFAULT_TEXT_UNDERFILLED — default_text ShoppingAd = 31 свободный символ (Fix 4, 2026-07-10)
- Симптом (прогон 59581fdd9f9d, tp5 712665815): «Текст по умолчанию» = «Авто в кредит на выгодных
  условиях. Оставьте заявку!» — 50 символов, 31 свободный (допустимо ≤81). Мало содержательных УТП.
- Где: **`create_set_gallery.py`** (~строка 49) — `body_text` брался из `it.get("texts")[0]` или
  `tpl_texts[0]`; при деградации AI → короткий аварийный fallback из `ai_agents.py`.
  **`create_set_assets.py`** — константа `SHOPPING_DEFAULT_TEXT` отсутствовала.
- Root-cause: D7 (ERRORS_JOURNAL K4_CONTENT_STYLE_WEAK_STATIC_RESERVES) задокументировал проблему
  коротких аварийных текстов. Уплотнение аварийных текстов в `ai_agents.py` (🟡 ждёт деплоя) ПОМОГАЕТ
  для LLM-fallback, но per-group texts[0] мог по-прежнему давать короткий текст при любом degraded
  контексте. ShoppingAd нужен ОДИН общий переиспользуемый текст, заполненный под лимит (DOD §2.4).
- Решение (2026-07-10):
  - `create_set_assets.py`: добавлена константа `SHOPPING_DEFAULT_TEXT = "Авто в кредит от 9 000 ₽/мес.
    Первый взнос 0 ₽. Одобрение за 30 минут."` (70 символов, ≤81, кредитный угол).
  - `create_set_gallery.py` (~строка 49): `body_text = SHOPPING_DEFAULT_TEXT` (import from
    create_set_assets), с fail-safe на `texts[0]`/`tpl_texts[0]` если импорт упал.
- Чинит ли delayed content_repair уже созданные РК (712665815): НЕТ. Нет механизма обновления
  default_text существующих ShoppingAd в delayed-repair цикле. `_campaign_default_text_repair`
  в `create_set_repairing.py` — STUB. Фикс только для будущих прогонов.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined). НЕ деплоено. live не
  проверено — проверит следующий прогон: default_text tp5/ShoppingAd = 70 символов, кредитный угол.
- НЕ помогло ранее: D7 (K4) уплотнение аварийных ai_agents-текстов — ОБЩЕЕ улучшение, но не
  гарантировало ОДИН конкретный текст под лимит для ShoppingAd по DOD.

### UAC_SITELINKS_PCT_FILTER_STARVES_6OF8 — при _title_has_pct=True 2 из 8 backup-филлеров
                                             отфильтровывались → 6 сайтлинков вместо 8 (Fix 2, 2026-07-10)
- Симптом (прогон 59581fdd9f9d): tp7 712664634 и 712664590 — `UAC_SITELINKS_MISSING actual=6
  expected=8`. При `_title_has_pct=True` (заголовки содержат %) из 8 генерик-филлеров 2 содержат
  «%» (позиции 5-6 «Выгода до 45% при покупке» и «Господдержка до 20%») → фильтр
  `_sitelink_has_pct` выбрасывает оба → остаётся 6.
- Где: **`blueprint.py:_GENERIC_SITELINK_FILLERS`** (~строка 5960) и
  **`create_set_master_product.py`** (~строки 256-291) цикл сборки с `_title_has_pct and
  _sitelink_has_pct` фильтром.
- Root-cause: `_GENERIC_SITELINK_FILLERS` содержал 8 филлеров, из которых 2 (позиции 5 и 6)
  содержат «%». При `_title_has_pct=True` → после фильтра остаётся 6. Недостаточный запас.
  Архитектура: K4/D1 исправил тексты, но не добавил резервный буфер без-% для этого сценария.
- Решение (2026-07-10, blueprint.py после строки 6073):
  Добавлены 2 backup-филлера без «%» с явным комментарием «позиции 9-10, используются когда
  _title_has_pct=True фильтрует позиции 5-6»:
  `{"title": "Рассрочка без переплат", "description": "Оформим рассрочку без скрытых платежей и комиссий"}`
  `{"title": "Гарантия на автомобиль", "description": "Расширенная гарантия при покупке нового автомобиля"}`
  title 22 chars ≤30, desc 49-50 chars ≤60. Оба без «%».
  При `_title_has_pct=True`: из 10 фильтруется 2 (поз.5+6) → остаётся 8 валидных.
- Чинит ли delayed content_repair уже созданные РК (712664634, 712664590): НЕТ. `UAC_SITELINKS_MISSING`
  по DOD §1.b = warn (fixable:False). Delayed-repair не перезаписывает UAC-сайтлинки существующих РК.
  Фикс только для будущих прогонов (при создании новых UAC tp7).
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined). НЕ деплоено. live не
  проверено — проверит следующий tp7-прогон: `sitelinks_count=8` при `_title_has_pct=True`.
- НЕ помогло ранее: K4/D1 заменил тексты 8 филлеров — не решал проблему нехватки при фильтрации.

### BRAND_NOT_FIRST_FALSE_POSITIVE_OBSHEE — BRAND_NOT_FIRST на «Общее»-кампаниях → UNFIXABLE (Fix 5, 2026-07-10)
- Симптом (прогон 59581fdd9f9d): `BRAND_NOT_FIRST×2` на кампаниях 712665480 и 712666720 (типа
  «Общее»-КС). 4 попытки fix_brand_not_first → `UNFIXABLE`. Заголовки не содержат бренд — потому что
  для «Общее» (`ct0000`) бренда быть НЕ должно. Ложный детект.
- Где: **`campaign_spec_audit.py:_audit_brand_not_first`** (~строки 1052-1118), loop по группам.
- Root-cause: функция фильтровала только `ct == "ct0000"` (skip). Но «Общее»-кампании имеют группы
  с ct0010 (Дром), ct0014 (Авто/Автомобили) — НЕ ct0000. Эти ct есть в `_ag_part1_map` (тема-агрегаты
  из gsheet_naming), которая для них возвращает «Дром», «Авто» и т.п. → `agid_to_brand["gid"] = "Дром"`.
  Заголовки группы (ct0010/ct0014) не начинаются с «Дром» → `brand_head_ok=False` → `BRAND_NOT_FIRST`.
  Фиксер `fix_brand_not_first` пытается переписать заголовки под «Дром в начале» — невалидно (Дром не
  марка). 4 попытки провала → `UNFIXABLE`.
- Решение (2026-07-10, campaign_spec_audit.py строки ~1058-1075):
  Добавлен фильтр по `_ct_segment`: перед добавлением группы в `agid_to_brand` — проверяем
  `_ct_segment_fn(ct) in ("Марки", "Модели")`. Если «Общее» — `continue` (пропустить).
  Fail-safe: если `_ct_segment` недоступен в `_DEPS` — пропускаем фильтр (ведём себя как раньше).
  Smoke-test (изолированный): ct0010-«Дром» → пропущен; ct0000 → пропущен (гейт ct0000); ct0031
  «Changan» (Марки) → агрегирован. `agid_to_brand = {"333": "Changan"}` — правильно.
- Чинит ли delayed content_repair уже созданные РК (712665480, 712666720): ДА (частично).
  Следующий audit-цикл delayed_repair вызовет `_audit_brand_not_first` с новым кодом — ложный
  `BRAND_NOT_FIRST` не сгенерируется → `UNFIXABLE`-статус не возникнет → delayed-repair перестанет
  тратить попытки впустую. Уже имеющийся `UNFIXABLE` в result не отзывается ретроактивно, но
  следующий delayed-repair цикл будет чист.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined; smoke-test PASS). НЕ деплоено.
  live не проверено — проверит следующий прогон/delayed-repair: BRAND_NOT_FIRST не должен появляться
  на «Общее»-кампаниях (ct0010/ct0014 не в проверке).
- НЕ помогло ранее: —

### DCR_CONTENT_REPAIR_HOLDS_PARENT_RUNNING — родитель висит running ~час на фоновой добивке dcr (ТРЕК A, 2026-07-10)
- Симптом (прогон 59581fdd9f9d): 14 РК созданы за 41 мин, потом джоба ещё ~час висела `status=running`
  с `done=14/14`, пока не стала `interrupted` (рестарт сервиса). НЕ finalize-freeze (#17/R2-1 тут ни
  при чём — воркер СВОБОДЕН, блокирован только СТАТУС карточки).
- Где: `blueprint.py:_schedule_delayed_content_repair_after_done` (~1822) → `_parent_absorb_child_start
  (parent_job_id, f"dcr:{did}", 0)`; watchdog `_create_watchdog_tick` (~868 `if _active_children:
  continue`); терминал dcr `_run_delayed_content_repair` (~2046) + K1-watchdog (~2168).
- Root-cause: после создания+аудита джоба УЖЕ `done`. Done-блок воркера планирует delayed
  content_repair (dcr) и `_parent_absorb_child_start` ФЛИПАЛ родителя обратно в `running` +
  `_active_children=["dcr:…"]`. Watchdog ЛЕГИТИМНО щадит такую джобу (для реальных recreate/UAC/finalize
  так и надо), но dcr — это ОТЛОЖЕННАЯ фоновая добивка (run_at + свой демон-исполнитель), а НЕ дочерний
  воркер. dcr застревал в `partial` (keywords_repair не мог дозалить ключи → reschedule до кап
  `_DELAYED_REPAIR_MAX_RESCHEDULES`), каждый reschedule оставлял родителя `running` → карточка висела
  ~час. Дизайн-конфликт: фаза «докрутка» в UI ВЫКЛючена (F async-finalize OFF), но dcr всё равно держал
  родителя не-терминальным.
- Решение (2026-07-10, ТРЕК A — вариант 1 «детач», обоснование: минимальнее и согласуется с «докрутка
  не в UI» — родитель доходит до ТЕРМИНАЛА после создания+аудита, dcr крутится демоном асинхронно):
  - Флаг `_DCR_DETACH_PARENT` (env `DIRECT_DCR_DETACH_PARENT`, дефолт **ON**; реверс=0).
  - `_schedule_delayed_content_repair_after_done`: под детачем НЕ зовём `_parent_absorb_child_start` для
    dcr → родитель остаётся `done`; в result `delayed_content_repair_scheduled.parent_detached=True`.
    dcr-строка сохраняется и исполняется демоном как раньше (reschedule/«до нуля» не тронуты).
  - `_parent_absorb_child_progress`: ранний no-op при `child_jid.startswith("dcr:")` → терминальные
    absorb-вызовы dcr (успех/ошибка/K1-watchdog) НЕ двигают бар и НЕ ВОСКРЕШАЮТ уже-терминального
    родителя (done/cancelled/error/interrupted) в `done`.
- Не сломано: реальные дочерние докрутки (recreate/UAC-replace/resume — child_jid=job_id, absorb_start
  на старте воркера ~2591) и finalize (`fin:…`, ~2669) — child_jid НЕ начинается с `dcr:` → guard их не
  трогает, ими рулят K1/F watchdog'и как прежде. Watchdog done>=total-таймаут (R2-1) и done<total
  (`_CREATE_RUNNING_TIMEOUT`) без изменений. `_reconcile_parent_job_counters` (счётчики) не трогает
  статус → под детачем работает штатно. `_job_db_progress` (line 644) — только для реальных child-джоб.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined в blueprint). НЕ деплоено. **live не
  проверено** — проверит прогон: набор после создания+аудита флипается в `done` СРАЗУ (не висит ~час);
  content_repair виден в result как `delayed_content_repair_scheduled` и дожимается демоном отдельно;
  реальные recreate/finalize по-прежнему держат карточку running до завершения.
- НЕ помогло ранее: R2-1 (finalize-таймбокс) и #17 (постпроцесс-таймбокс) чинили ФАЗУ финализации
  СОЗДАНИЯ (воркер блокирован на сокете) — здесь воркер СВОБОДЕН, висел только СТАТУС из-за dcr-absorb.
  K1-watchdog (dcr running>30мин→failed) закрывал СТРОКУ dcr, но при `partial`+reschedule строка
  жива/не-running → K1 её не трогает, родитель оставался running.

### DELETE_DRAFTS_FREEZE_IN_CREATE_POSTPROCESS — джоба удаления заходила в create-done-блок → морозила воркер (R2-5, 2026-07-10)
- Симптом: джоба `kind=delete_drafts` после «удалено 14/14» висит `running` 2+ мин (воркер CPU 0.3%,
  лог молчит) — тот же постпроцесс-фриз, что у создания. Для УДАЛЕНИЯ постпроцесс бессмыслен —
  удалять нечего верифицировать/финализировать.
- Где: `blueprint.py:_create_worker_loop`. Для delete_drafts `data = _delete_drafts_core(...)`
  (удаление, БЕЗ create-постпроцесса) — корректно. НО дальше done-блок `if final_status == "done":`
  (~2655) исполнялся для ЛЮБОЙ done-джобы, включая delete: `_auto_queue_recreate_after_done` +
  `_schedule_delayed_content_repair_after_done` + finalize-очередь (`unregister`/`enqueue`/
  `run_finalize_job` — Grid СЕТЕВЫЕ вызовы). Для delete всё это мусорно и вешало поток на
  подвисшем recv() финализации несуществующей РК.
- Root-cause: done-блок не отличал delete от create — гейт был только по `final_status == "done"`.
  `_delete_drafts_core` сам постпроцесс не звал (это `_create_set_response`), но done-блок воркера —
  общий для всех kind → delete проваливалась в create-финализацию/добивку.
- Решение (2026-07-10, R2-5): в `_create_worker_loop` после клейма вычисляется
  `_is_delete_drafts = (body or {}).get("_kind") == "delete_drafts"`; гейт done-блока изменён на
  `if final_status == "done" and not _is_delete_drafts:`. Delete-джоба после `_delete_drafts_core`
  и `_job_db_save(full=True)` (терминал `done` уже записан ДО блока) идёт сразу в `finally`
  (release слота), минуя auto-recreate/delayed-repair/finalize. Create-путь (`kind=set`) не тронут
  байт-в-байт — тот же блок, тот же порядок; финализ-register в начале цикла для delete безвреден
  (OFF→no-op, идемпотентный unregister в finally). #21 finalize-watchdog не тронут.
- UI-лейбл: НЕ баг фронта. Фронт уже ветвит по `j.kind==='delete_drafts'` во всех состояниях
  («удалено черновиков», не «создано»/«финализация»), баннер (index.html:3757) уже предпочитает
  `result.deleted`; kind персистится в body._kind → переживает поллинг/recovery (`_job_kind`).
  Конвенция `created=deleted` в result используется delete-веткой фронта (3678/3706) → менять
  бэкенд-поле НЕЛЬЗЯ (сломает delete-дисплей). `deleted` уже отдельным полем есть. Follow-up только
  если реальный мислейбл всплывёт live.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined). НЕ деплоено. **live не проверено** —
  проверит прогон: delete_drafts после «удалено N/N» флипается в `done` сразу, воркер не морозится,
  create-джобы (постпроцесс/добивка/finalize) без изменений.
- НЕ помогло ранее: #17 (таймбокс постпроцесса) + R2-1 (finalize-watchdog) чинили ФАЗУ финализации
  СОЗДАНИЯ; delete в неё вообще не должна была заходить — здесь корень «зашла и зависла» устранён
  гейтом, а не таймбоксом.

### R2-4_QUALITY_BUNDLE — Э-ключи + каталог-негатив + UAC-сайтлинк-дубль + разнобой сумм (R2-4, 2026-07-10)
Бандл из 4 остаточных дефектов прогона e05fbc86e8ca (нашёл Семён). Свои файлы, НЕ деплоено, live не проверено.

- **(а) Geely Cityray/Coolray через «э» протекали в «Общее».**
  - Симптом: tp5 «Общее» ct0010-Дром (712650300) получила ключ «ситирэй» (Э, U+044D). В `_AUTO_BRAND_
    CYRILLIC_EXTRA` были только «ситирей»/«кулрей» (Е, U+0435) → D8-дискриминатор `_auto_brand_tokens`
    не распознавал Э-вариант как модель-токен → чистый модель-запрос протёк в тема/«Общее»-группу.
  - Root: «рей»(е, U+0435) ≠ «рэй»(э, U+044D) — разные Unicode-точки; у Geely оба написания в кабинетах.
  - Фикс: `text_gen.py:_AUTO_BRAND_CYRILLIC_EXTRA` += «ситирэй»,«кулрэй»,«рэй» рядом с Е-вариантами.
    Верифицировано изолированно: `_auto_brand_tokens()` содержит все 6 (ситирей/ситирэй/кулрей/кулрэй/рей/рэй).
  - Почему прежнее не сработало: D8 (2026-07-09) добавил только Е-написание — Э-вариант остался слепым.

- **(б) ListingAd брендовой группы — НЕГАТИВ вместо ПОЗИТИВА.**
  - Симптом: «Страницы каталога» брендового tp1/tp5 показывали «Название каталога НЕ содержит
    knewstar,moskvich,omoda,…» (176/198 стр.) вместо «содержит {своя марка}». Для BAIC-группы нужен
    «содержит BAIC», не «не содержит 8 чужих».
  - Root: `create_set_tp1_builders._grid_add_listings_with_name_filters` добавлял к позитивному name-
    фильтру брендовой группы негативный `_lad_minus_conds` (NOT_CONTAINS_ALL глоб-минус-марок) как
    `extra_conds`. Позитив CONTAINS уже ограничивает страницы своей маркой → негатив избыточен; а при
    падении позитива (D4) в кабинете оставался ТОЛЬКО негатив.
  - Фикс: брендовая группа (`_val` есть) → ПОЗИТИВНЫЙ name CONTAINS_ANY [своя марка] ТОЛЬКО, negative
    extra_conds убран. Общее/ct0000 (`_val=None`) — негатив-глоб-минус сохранён без изменений
    (весь каталог минус чужие). Cookie-путь листинга (`:1742`) уже был позитив-only — не тронут.
  - ⚠️ D4-follow-up: для AUTO_RU фида **3537034** (yandex.xml) поле листинга не резолвится в
    fieldsForUseAs (ни name, ни folder_id) → позитив-фильтр для ЭТОГО фида невозможен (набор кандидатов
    в `set_listing_name_filters` исчерпан → GridFinalizeError, `listing_name_set` низкий). Это **честный
    follow-up**: листинг-фильтр для фида 3537034 недостижим текущим API — нужен маппинг реального
    текстового поля каталога или отказ от name-фильтра для этого формата. Не фейкаем негативом.
  - Почему прежнее не сработало: UNAVAILABLE_FIELD_LISTING_FILTER (D4) резолвил ПОЛЕ, но не убирал
    негатив у брендовой группы → при провале позитива негатив всплывал в кабинет.

- **(в1) UAC-сайтлинки мимо topic/semantic-дедупа.**
  - Симптом: tp7 (UAC) быстрые ссылки содержали ДУБЛЬ «Платеж от 9 000 ₽ в месяц» + «Автокредит от
    9 000 ₽/мес» (обе — один оффер credit_pay|9000).
  - Root: `create_set_master_product` собирал 8 сайтлинков с дедупом ТОЛЬКО по ТОЧНОЙ строке title+desc
    (`_seen_sl_keys`). `_dedup_sitelinks` (bucket-лимиты + `_sitelink_semantic_key`) вызывался ТОЛЬКО в
    ветке `<8` → при полном комплекте 8 не отрабатывал = «UAC мимо дедупа». bucket credit≤2 пропускал
    обе как разные строки, хотя `_coherent_payments` свёл суммы к 9000 → семантически дубль.
  - Фикс: `_dedup_sitelinks(diversify_sitelink_utp(...))` вызывается ВСЕГДА (не только при <8); недобор
    восполняет ветка `<8` из филлеров. Верифицировано: точные прод-строки «Платеж от 9 000 ₽ в месяц»/
    «Автокредит от 9 000 ₽/мес» → оба `credit_pay|9000` → второй схлопнут.
  - Почему прежнее не сработало: `_dedup_sitelinks` был под гейтом `<8` → полный набор его не проходил.

- **(в2) Reuse ОДНОГО набора сайтлинков на аккаунт (флаг, дефолт OFF).**
  - Мотив (Семён): tp1 и tp7 имели РАЗНЫЕ сайтлинки; хочет один согласованный набор на аккаунт.
  - Реализация (по аналогии content-reuse #16): `ai_content._account_sitelinks_get/put(login)` —
    login-scoped кэш на проход воркера, первый полный (≥8) набор эталон. Wired в orchestrator (tp1/tp2/
    tp5) + master_product (tp6/tp7). Эталон re-фильтруется per-item гардами (pct/BU/лимит) → несовместимый
    → остаёмся на своём наборе (brand-first/лимиты целы). Флаг `DIRECT_SITELINK_REUSE_ACCOUNT` **дефолт
    OFF** (кросс-tp риск) — opt-in, Семён включит и проверит.

- **(г) Разнобой сумм платежа между объявлениями.**
  - Симптом: «Платеж от 9 000» (одно объявл.) vs «Кредит от 12 000 ₽/мес» (default_text) в одном аккаунте.
  - Root: `unify_utp_numbers`/`_coherent_payments` унифицируют сумму ТОЛЬКО ВНУТРИ item (канон = первое-
    встреченное) → у каждого item свой первый-встреченный → аккаунт-разнобой.
  - Фикс: `ai_content._account_pay_unify(login,…)` — КАНОН-сумма на аккаунт (первая валидная 9-15к,
    зафиксирована, применяется ко всем через доверенный `text_gen._apply_payment_amount`). Wired в оба
    choke-point (orchestrator + master_product). Флаг `DIRECT_PAY_CANON_ACCOUNT` **дефолт ON** (низкий
    риск: та же атомарная замена что в `_coherent_payments`, только канон общий). Верифицировано: item1
    задал 12000 → item2 «9 000» → унифицирован к «12 000».
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined; изолированные smoke а/в1/г зелёные).
  НЕ деплоено. **live не проверено** — проверит round 2.

### TITLE_BRAND_ORDER_UPGRADE_CLOBBER — `_upgrade_credit_titles` затирал brand-first короткими credit-first вариантами (R2-3, 2026-07-10)
- Симптом (прогон e05fbc86e8ca, tp1 BAIC 712648428, СВЕЖАЯ генерация): заголовки НЕ brand-first —
  «Кредит на BAIC в Кемерово. Первый взнос 0 ₽» (марка после УТП), «Платеж от 9 000 ₽/мес. BAIC в
  Кемерово» (марка во 2-м сегменте) + 13-18 свободных символов (короткие). Промпт-правило brand-first
  и reorder в `_rsya_titles` НЕ гарантировали результат live.
- Где: **`create_set_assets.py:_upgrade_credit_titles`** (вызывается из `_responsive_ad` — ФИНАЛЬНАЯ
  сборка Titles на TOKEN-пути v501 ads.add: tp1 `create_set_tp1_builders.py:328`, tp2/tp4
  `create_set_text_builders.py:127`).
- Root-cause: `_upgrade_credit_titles` при `_needs_credit_title_upgrade=True` ЗАМЕНЯЛ brand-first
  g_titles (из `_rsya_titles`) своими вариантами `f"Кредит на {anchor}. Первый взнос 0 ₽"` /
  `f"Платеж от 9 000 ₽/мес. {anchor}"` — марка ПОСЛЕ УТП (не подлежащее) И короткие (без `_fill_title`,
  ~43 симв). Это ТОЧНО симптомы FACT. Прежний reorder в `_rsya_titles` давал brand-first g_titles, но
  `_upgrade_credit_titles` их клобберил ПОСЛЕ → почему «reorder не сработал live». Cookie-путь (grid
  `build_ad._fill_titles`) upgrade НЕ вызывает → клобберил только TOKEN-путь (прогон при 40М баллах = token).
- Решение (2026-07-10, R2-3) — ДЕТЕРМИНИРОВАННО, без опоры на LLM:
  - **`text_gen._brand_first_reorder(title, brand)`** (новый): сегментный реордер — марка/модель
    принудительно в НАЧАЛО (до первой точки). «Кредит на BAIC в Кемерово. …» → «BAIC в Кемерово.
    Кредит. …»; «Платеж … BAIC в Кемерово» → «BAIC в Кемерово. Платеж …». Уже-ведущая марка и допустимые
    модификаторы («Новый/Купить BAIC») — без изменений; brand пустой (Общее/ct0000) → no-op; марки нет
    вовсе → как есть (страховка = LLM `fix_brand_not_first`). Guard: реордер, породивший `_bad_ad_title`/
    `_is_bad_start`, откатывается на оригинал.
  - **`_rsya_titles`** (choke-point ОБОИХ путей, tp1/tp2/tp4 group titles): реордер применён в главном
    цикле + в `brand_fillers`-нормализациях для брендовых групп → g_titles ВСЕ brand-first. Шаблон
    `_brand_title_set` «Кредит на {brand}…» переписан на «{brand}. Выгода …» (brand-first в источнике,
    покрывает и tp6/tp7 standalone).
  - **`_upgrade_credit_titles`** (TOKEN финал): брендовые варианты переписаны на brand-first
    `f"{anchor}. Кредит …"`; в цикле каждый (варианты + pass-through seq) прогоняется через
    `_brand_first_reorder` + `_fill_title` (§2.2: добивка до ≥54, ≤2 свободных).
- Оба пути покрыты: TOKEN — `_responsive_ad`→`_upgrade_credit_titles` (фикс) + g_titles brand-first;
  COOKIE — `_rsya_titles`→grid `build_ad._fill_titles` (preserve, upgrade не зовётся). Backup —
  `fix_brand_not_first` через delayed content_repair (после R2-1/К1 фикса фриза реально доедет,
  `blueprint.py:7979`, `_audit_brand_not_first` детект).
- Не сломано: Общее/ct0000 (brand пуст) — реордер no-op; дедуп УТП §2.3 (варианты distinct-бакеты);
  ≤56 (guard `_trim_ad_line`); чужие марки (реордер по `_own_brand_tokens` своей марки); tp5 ShoppingAd
  (без TextAd) н/п; tp3/tp6/tp7 вне scope.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 новых undefined; изолированные тесты: FACT-симптомы
  BAIC/Haval/Chery → 0 нарушений brand_head_ok И lead, len 48-56; `_responsive_ad` token-финал 0/7
  нарушений; Общее-ветка валидна). НЕ деплоено. **live не проверено** — проверит round 2 (tp1/tp2/tp4:
  КАЖДЫЙ заголовок марка в 1-м сегменте, ≤8 свободных).
- НЕ помогло ранее: reorder ТОЛЬКО в `_rsya_titles`/промпт brand-first — `_upgrade_credit_titles`
  (финал token-пути) клобберил результат ПОСЛЕ → live оставался non-brand-first. R2-3 чинит именно
  финальную сборку + держит реордер детерминированным на обоих путях.

### FALSE_152_COOKIE_FLIP_AT_LIVE_OPERATOR_UNITS — ложный/транзиентный 152 уводил набор на куку при 40М баллов (R2-2, 2026-07-09)
- Симптом (прогон e05fbc86e8ca, agency victorylotsofads1 = **40.5 МЛН** баллов оператора): транзиентный/
  ложный 152 увёл tp5 «Марки» на via_cookie → сегменты кука не умеет → `NO_BRAND_SEGMENTS` → деферред.
  Самозалечилось token-ретраем, но это лишний детур + риск потери. При 40М баллов реального 152 быть НЕ
  может → 152 был ЛОЖНЫЙ.
- Где: `create_set_orchestrator.py` — (а) МИД-СЕТ флип канала на куку (`_run_channel`, блок «152→via_cookie»
  ~597); (б) КОНЕЦ-СЕТ куки-remainder enqueue (`_units_block`, `_cb["via_cookie"]=True` ~1112). Обе точки —
  «флип на куку по 152».
- Root-cause: строгий флип из В2 (`API_FIRST_STREAK`/`_units_alive_for_login`) стоял ТОЛЬКО под флагом
  `DIRECT_API_FIRST=ON`, а **прод бежит OFF** → исполнялась OFF-ветка, которая флипала на куку по ПЕРВОМУ
  units-маркеру БЕЗ какой-либо проверки остатка баллов. Плюс даже без мид-флипа конец-сет `_units_block`
  БЕЗУСЛОВНО ставил куки-джобу для остатка. Т.е. В2 не удержал флип, потому что В2 = только ON, а прогон = OFF.
- Решение (2026-07-09, R2-2):
  - **Мид-сет флип** (обе ветки OFF и ON): перед куки-флипом по 152 ЖЁСТКО читаем остаток баллов оператора
    `_units_alive_for_login(login, agency)`. Флип на куку ТОЛЬКО при `_confirmed_dead` (прочитано УСПЕШНО И
    rest < порога → `is False`) ИЛИ (ON) streak≥2 реальных 152 подряд. Живые баллы (True) → ложный 152 → НЕ
    флипать. None (не прочитали: сеть/блип/нет токена) → НЕ трактовать как «мертвы» → НЕ флипать. Ошибка **53
    (auth)** отделена (`_new_auth_fail`) — она НЕ про баллы, флипает на куку без units-проверки (как раньше).
  - **Конец-сет `_units_block`**: тоже читаем остаток. Живые/None → остаток в TOKEN-деферред СРАЗУ
    (`_resume_via_token=True`, resume_at=now(); `exclude_id`=parent `_deferred_id` от self-reference,
    инцидент SEGMENT_TP5) — демон повторит API-путём, БЕЗ куки. Куки-remainder — ТОЛЬКО при
    `_units_dead_confirmed` (реальный 152) ИЛИ `_rc >= _RESUME_MAX` (исчерпан токен-ретрай = законный
    куки-фолбэк).
- Cookie/UAC сохранены: реальный 152 (rest<порога прочитано) → куки-remainder как раньше; error 53 → кука;
  tp6/tp7 UAC не тронуты (они ВСЕГДА кука by design, вне этого блока); сегментный tp5 при реальном 152 →
  token-деферред (defer_keep/exclude_id, первый фикс сессии) не сломан.
- Статус: 🟡 код на Mac (py_compile OK, pyflakes 0 undefined). НЕ деплоено. **live не проверено** — проверит
  round 2: транзиентный 152 при живых баллах НЕ уводит tp5 «Марки» на куку (нет NO_BRAND-детура), остаток
  добивается токеном; реальное исчерпание (rest≈0) по-прежнему уходит на куки-remainder.
- НЕ помогло ранее: В2 (`API_FIRST_STREAK`, строгий флип) — держал флип ТОЛЬКО под DIRECT_API_FIRST=ON;
  прод OFF → OFF-ветка флипала по первому маркеру без units-проверки. R2-2 распространяет жёсткую
  units-проверку на ОБЕ ветки (OFF-дефолт и ON) и на конец-сет remainder.

### FALSE_152_TOKEN_RETRY_TECH_FAIL_FLIPS_COOKIE — техошибка token-ретрая при ЖИВЫХ баллах даёт ложный «баллы исчерпаны» + куку (2026-07-13)
- Симптом (job porg-asfbs7qe, агентство victoryagency14, остаток ~37.8 МЛН баллов): ложный попап
  «⛔ Баллы коммандера исчерпаны (error 152)» на конец-сет remainder, хотя баллы ЖИВЫ. Плюс остаток
  уводился на via_cookie → сегментный tp5 теряет функциональность (`NO_BRAND_SEGMENTS_AVAILABLE`).
- Где: `create_set_orchestrator.py`, конец-сет блок `_units_block` (гейт куки-remainder, ~1190). Дырка
  в инварианте R2-2 (см. запись FALSE_152_COOKIE_FLIP_AT_LIVE_OPERATOR_UNITS выше).
- Root-cause: token-ретрай (`_deferred_save` с `_resume_via_token=True`, строки 1171-1184) обёрнут в
  `try/except: _token_retry_did = None`. Любое ТЕХНИЧЕСКОЕ исключение (DB-хиккап, не units) тихо оставляло
  `_token_retry_did = None`. Гейт куки-remainder был `if _remaining and not _token_retry_did:` — он НЕ
  перепроверял `_units_dead_confirmed`/`_rc`, а трактовал «`_token_retry_did` is None» как «баллы
  исчерпаны ИЛИ ретраи исчерпаны». При живых баллах (`_units_dead_confirmed=False`) и `_rc < _RESUME_MAX`
  это ЛОЖЬ → проваливался в куки-фолбэк + текст «баллы исчерпаны». То есть инвариант R2-2 («кука ТОЛЬКО
  при dead ИЛИ rc>=max») обходился именно техническим падением ретрая.
- Решение (2026-07-13): (1) гейт куки-remainder ужесточён до
  `if _remaining and not _token_retry_did and (_units_dead_confirmed or _rc >= _RESUME_MAX):` — кука
  строго по инварианту R2-2. (2) Добавлена честная ветка `elif ... not _units_dead_confirmed and
  _rc < _RESUME_MAX:` для «ложный 152 при живых баллах + техпадение ретрая»: повторная TOKEN-постановка
  остатка (`_deferred_save`, `_resume_via_token=True`, БЕЗ `via_cookie`) → на успех honest-note
  «повторно на докрутку токеном», на неудачу — флаг `_false152_unplaced` + honest-note «баллы ЕСТЬ,
  техошибка, нужен ручной перезапуск» (НЕ «исчерпаны»). (3) `_false152_unplaced` в parent-handling:
  при resume-run с непоставленным остатком родительская строка → `waiting` (демон повторит токеном),
  НЕ гасится в `done` — инвариант «пункты не теряются» (тот же класс, что инцидент SEGMENT_TP5 08.07).
  Реальный 152 (`_units_dead_confirmed=True`) и путь rc>=max — не тронуты.
- Статус: 🟡 код на Mac (py_compile OK, pyflakes 0 новых undefined). НЕ деплоено. **live не проверено** —
  проверить: технический сбой token-ретрая при живых баллах НЕ даёт «баллы исчерпаны» и НЕ уводит на
  куку; реальное исчерпание (rest≈0) и rc>=max по-прежнему идут на куки-remainder.
- НЕ помогло ранее: R2-2 (FALSE_152_COOKIE_FLIP) — ввёл units-проверку и token-деферред при живых баллах,
  НО оставил гейт куки-remainder на голом `not _token_retry_did`, из-за чего ТЕХНИЧЕСКОЕ падение ретрая
  (а не units) всё равно уводило на куку. Текущий фикс закрывает эту щель, перепроверяя dead/rc в самом
  гейте.

### FALSE_152_DEFER_LABEL — пустой контент-пак (defer) мислейблится как units → ложное «Баллы исчерпаны» (2026-07-14)
- Симптом (job 869432bff03b, по живым логам): попап «⛔ Баллы коммандера исчерпаны (error 152)», хотя
  реального 152 НЕ было. tp2 отложились из-за пустого контент-пака (`defer:true, error:"пак M3 пуст"`).
  Баллы агентства (оператора) тратились корректно (`Use-Operator-Units: true`), рассинхрона нет.
- Где: (1) `create_set_text.py:~83` — маршрутизация token→cookie фолбэка tp2/tp4;
  (2) `create_set_orchestrator.py:~1261` — финальная подпись units-блока (catch-all при `_pend=0`).
- Root-cause: (1) `_tok_units = bool(res.get("defer") or _is_units(res.get("error")))` приравнивал
  `defer` (пустой пак; token-путь `create_set_feed_builders.py:386-395` ставит `defer` на `skipped`/нет
  групп, error="tp2(token) не дозаполнена: пак пуст" — НЕ матчит UNITS_RE) к units. Из-за этого
  `res["token_units_fallback"]=True` на КАЖДЫЙ defer → orchestrator (`_run_item` скан ~579) считал это
  реальным 152 (`_new_units_fail=True`), накапливал `units_fail_streak` → при `_API_FIRST` и ≥2 defer
  подряд `_streak_confirmed` → `_units_switched=True` → ложная подпись «Баллы исчерпаны во время набора»
  (~1265). (2) catch-all `elif units_note is None:` при `_pend=0` безусловно писал «⛔ Баллы коммандера
  исчерпаны … нет несозданного остатка», хотя реальное исчерпание в этой ветке не подтверждено
  (`_units_dead_confirmed` ставится только при непустом `_remaining`).
- Решение (2026-07-14, scoped, 2 файла): (1) `_tok_units = bool(_is_units(res.get("error")))` — units-метку
  ставим ТОЛЬКО на реальный 152 в тексте ошибки; `defer` (пустой пак) БОЛЬШЕ не ведёт к
  `token_units_fallback`. Логика defer/докрутки не тронута: cookie-фолбэк (line 84) и `if not ok and not
  defer: add_job_err` (line 117) прежние → defer по-прежнему уводит пункт в docrutka. (2) catch-all
  разветвлён: при `_pend=0` пишем честный текст (есть defer-пункты → «часть отложена на докрутку,
  контент-пак пуст — это не исчерпание баллов»; иначе → «всё создано/добито»), при `_pend>0` (реальный
  несозданный остаток) — прежнее «⛔ Баллы коммандера исчерпаны … повторите после сброса». Реальный 152
  (`_units_block=True` из 152-текста результата + `_pend>0`, и ветки auto_cookie/deferred ~1252-1260) не
  тронут; token-retry-tech-fail ветки (`FALSE_152_TOKEN_RETRY_TECH_FAIL`) не тронуты.
- Статус: ✅ ПОДТВЕРЖДЕНО МАСШТАБОМ по итоговому эффекту (2026-07-14, 4 акка / 56 кампаний):
  0 ложных «152», 0 defer, все tp2 dmp создались (устранён апстрим-корень — desync ct пака).
  ⚠️ Честно: сама ветка «defer≠units» так и НЕ была задействована ни в одном из 4 прогонов
  (пак полный → ничего не отложилось) → мислейбл-фикс live на defer-подписи по-прежнему НЕ проверен.
  Структурно фикс верен; для полной верификации подписи «отложено на докрутку» нужен прогон с
  реальным defer (пустой пак другого слепка).
- НЕ помогло ранее: — (первая правка развязки «defer≠units»). Смежные `FALSE_152_COOKIE_FLIP`/
  `FALSE_152_TOKEN_RETRY_TECH_FAIL` закрывали ложный 152 от РЕАЛЬНОГО units-маркера/техпадения ретрая,
  но НЕ ловили источник «defer помечен как units» на входе — эта правка убирает мислейбл в корне.

### WORKER_FREEZE_POSTPROCESS_NO_TIMEOUT — постпроцесс морозит поток воркера (#17, 2026-07-09)
- Симптом (живой прогон 09.07, ДВАЖДЫ за прогон): direct-worker ЗАМЕРЗАЛ — процесс жив, **CPU 0%, лог
  молчит 6-8+ мин**, джоба висит `running`/`interrupted`, `done=14/14` но НЕ флипается в `done`. Второй
  раз — при СВЕЖЕЙ куке И уже задеплоенном M3 circuit-breaker → блок НЕ на M3 и НЕ на куке. 0% CPU +
  тишина = поток заблокирован на СЕТЕВОМ `recv()` БЕЗ таймаута. Сопутствующий лог:
  `[agency-gate] sweep fail-open: connection to 103.88.240.90:5432 timeout` (Victory-DB подвисает).
- Где: постпроцесс между созданием РК и флипом джобы в done. Точка №1 (главная, до флипа) —
  `create_set_orchestrator.py:987 run_create_set_postprocess` (внутри `_create_set_response`, т.е. до
  возврата `data` воркеру, поэтому `done=N/N` не флипается): `verify_create_set` +
  `_create_set_live_verification` (Grid по куке) + `rauto.execute_safe_post_create` (Grid-ремонт по куке).
  Точка №2 (done-блок `blueprint.py:~2593`) — `_auto_queue_recreate_after_done` /
  `_schedule_delayed_content_repair_after_done` / finalize-enqueue: DB-записи в Victory.
- Root-cause: (а) **Victory-DB БЕЗ statement_timeout.** `_victory_conn`/`_victory_conn_rw` имели только
  `connect_timeout=15` (ловит фазу коннекта), но РЕЗУЛЬТАТ запроса читался с сокета без предела —
  подвисший запрос/мёртвая сеть = вечный блок на `recv()` (0% CPU, тишина). (б) **Нет таймбокса на
  постпроцесс в целом:** даже с тайт-таймаутами на HTTP (Grid/UAC уже 40-180с) суммарная деградация
  тянулась минутами и морозила поток; K1-watchdog чинит только БД-строку delayed-repair (отдельный
  демон), сам ЗАБЛОКИРОВАННЫЙ поток воркера так не освобождается.
- Решение (2026-07-09, #17):
  - **DB (`blueprint.py`):** `_victory_conn`+`_victory_conn_rw` — добавлены `options="-c statement_timeout=
    120000"` (env `DIRECT_VICTORY_STMT_TIMEOUT_MS`, сервер сам рвёт подвисший запрос) + keepalives
    (`keepalives_idle=30,interval=10,count=3` → мёртвый сокет детектируется за ~60с ConnectionError, а не
    висит вечно). ВСЕ DB-операции сервиса идут через эти 2 хелпера → покрыты одной точкой. 120с — щедрый
    потолок для OLTP jobs/deferred (одиночные строки/JSONB), но КОНЕЧНЫЙ.
  - **Таймбокс (`create_set_postprocess.py`):** `run_create_set_postprocess` теперь тонкая обёртка —
    тело вынесено в `_run_create_set_postprocess_body`, исполняется в daemon-потоке, основной поток ждёт
    `join(_POSTPROCESS_TIME_BUDGET_SECONDS=600, env DIRECT_POSTPROCESS_BUDGET_SEC)`. Не уложились → возврат
    degraded-результата (все 4 ключа на месте, `live_verification` без `repair_plan` → авто-recreate не
    стартует, `postprocess_timeboxed` в результате) → orchestrator возвращает `data` → джоба флипается в
    терминал (done). Созданные РК не теряются; orphan-поток дожимает свой bounded-таймаут (HTTP ≤180с,
    DB ≤~120с) и умирает сам; добивку/верификацию подхватит delayed-репэйр демон свежей Grid-проверкой.
- Как джоба ВСЕГДА доходит до терминала: главный блок (точка №1) внутри `_create_set_response` теперь
  bounded таймбоксом 600с → `data` всегда возвращается → воркер флипает `status=done` (`blueprint.py:2576`)
  и `_job_db_save(full=True)` (2589) ДО done-блочных добивок; сами добивки (точка №2) — DB-bounded +
  best-effort try/except, и выполняются уже ПОСЛЕ терминального сохранения.
- Не сломано: нормальный постпроцесс (verify+safe-repair) — тот же код в `_run_..._body`, при быстром
  завершении `box["out"]` отдаётся 1:1. K1-watchdog (delayed content_repair), F finalize-очередь не
  тронуты. keepalives безвредны (лишь быстрее детектят мёртвый сокет). statement_timeout=120с не рвёт
  здоровые OLTP-запросы. batch-аспекты (`DIRECT_BATCH_ASPECTS`, прод off) вне таймбокса — осознанно
  (по умолчанию не исполняются; их Grid-вызовы уже best-effort try/except).
- Статус: ❌ #17 НЕ ПОКРЫЛ реальную точку фриза (прогон e05fbc86e8ca 09.07, >33мин, done=14/14) →
  добито R2-1 (см. ниже). #17-таймбокс сам по себе корректен (постпроцесс отпускается ≤600с), но
  фриз был ВНЕ обёрнутой функции.
- НЕ помогло ранее: K1-watchdog (running>30мин→failed+закрыть child) — освобождает ТОЛЬКО БД-строку
  content_repair отдельным демоном; ЗАБЛОКИРОВАННЫЙ на сокете поток воркера так не освобождается (висит,
  пока не сработает СОБСТВЕННЫЙ таймаут операции) → нужны тайт-таймауты на самих операциях + таймбокс.
  **#17 (таймбокс ТОЛЬКО `run_create_set_postprocess`)** — не покрыл фазу финализации ПОСЛЕ создания
  (promo/build_response/DB-хвост/орфан-лок постпроцесса) + слепое пятно watchdog'а (см. R2-1).

### WORKER_FREEZE_FINALIZE_WATCHDOG_BLINDSPOT — watchdog БЕЗУСЛОВНО щадил done>=total → вечный running (R2-1, 2026-07-09)
- Симптом (прогон e05fbc86e8ca 09.07, >33мин): джоба `running`/`interrupted`, `done=14/14`, CPU 0%,
  тишина; delayed content_repair НЕ отработал (brand-first/ключи не добились). #17 (таймбокс постпроцесса
  600с + statement_timeout + keepalives) УЖЕ задеплоен — не помогло.
- Где: `blueprint.py:_create_watchdog_tick` (~843). Точка фриза: фаза ФИНАЛИЗАЦИИ набора в
  `create_set_orchestrator.py` ПОСЛЕ создания (done=len(items) выставлен на строке ~930), ДО возврата
  data воркеру: `attach_or_create_promo` (~958, сеть, НЕ таймбокснута) → postprocess (~986, таймбокс
  #17) → `build_create_set_response` (~1170) → DB-хвост (deferred/units). Любой зависший сетевой read /
  лок / мёртвая Victory-DB тут вешал воркер, а done-флип (blueprint:~2576) не наступал.
- Root-cause: watchdog `_create_watchdog_tick` при `done>=total` делал БЕЗУСЛОВНЫЙ `continue`
  («почти-завершённую джобу не красим error») — задумано против ложного kill'а куки-бэкфилла (массовый
  skip: created не растёт, done доходит до total). Но это создало СЛЕПОЕ ПЯТНО: джоба, зависшая в
  фазе финализации ПРИ done>=total, была невидима watchdog'у НАВСЕГДА. #17 таймбоксил ТОЛЬКО
  `run_create_set_postprocess` (одна из ~4 операций фазы) → promo/build_response/DB-хвост и возможный
  орфан-лок постпроцесса оставались без предохранителя, а watchdog их не подхватывал.
- Решение (2026-07-09, R2-1):
  - **Отдельный КОНЕЧНЫЙ бюджет фазы финализации** `_CREATE_FINALIZE_TIMEOUT` (env
    `DIRECT_CREATE_FINALIZE_TIMEOUT`, дефолт 900с > postprocess-бюджета 600с). При `done>=total` и
    `now-heartbeat > _CREATE_FINALIZE_TIMEOUT` → терминал `status=done` (кампании СОЗДАНЫ → не error) +
    `_watchdog_done`/`cancel` + освобождение агентского слота (`_agency_gate_release`). heartbeat при
    done>=total заморожен на последнем item (per-item `_bump_item`) → `_stuck` = ровно длительность
    финализации. Куки-бэкфилл (массовый skip) укладывается в бюджет; реальный фриз терминируется.
  - **delayed content_repair добивается** best-effort из watchdog'а (`_schedule_delayed_content_repair_
    after_done`, ВНЕ `_CREATE_COND` — берёт `_CREATE_JOBS_LOCK`): осиротевший воркер мог не дойти до
    своего done-блока (blueprint:~2594). Идемпотентно (`_delayed_content_repair_save` дедуп по
    parent_job_id; absorb_child по child_jid) → повтор проснувшимся воркером безвреден.
  - **Анти-регрессия:** джобы с активной дочерней добивкой (`result["_active_children"]`, dcr:/fin:)
    ЛЕГИТИМНО держат родителя running с done>=total (absorb_child_start→running) — ими рулят K1/F
    watchdog'и; финализ-таймаут их ЯВНО пропускает (не убивает delayed-repair/finalize на 15-й минуте).
    Путь done<total не тронут: тот же `_CREATE_RUNNING_TIMEOUT=1200` (гейт перенесён ниже, байт-в-байт).
- Как гарантирован терминал на ЛЮБОМ пути (C1 вкл/выкл): фаза финализации ОДНА для обоих путей —
  каналы C1 влияют только на цикл СОЗДАНИЯ (done<total, покрыт `_CREATE_RUNNING_TIMEOUT` + heartbeat);
  постпроцесс/promo/build_response идут через ту же орк-«хвост»-секцию после join каналов. Watchdog —
  внешний поток → освобождает джобу даже если воркер намертво на сокете/локе. Терминал: done<total →
  error за ≤1200с; done>=total-фриз → done за ≤900с; активные dcr:/fin: → K1/F.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes 0 undefined в blueprint). НЕ деплоено. **live не
  проверено** — проверит round 2: при подвисе финализации watchdog флипает done ≤900с + ставит
  delayed content_repair; в result `finalize_timeboxed{stuck_seconds,budget_seconds}`; активная
  delayed-добивка НЕ убивается досрочно.
- НЕ помогло ранее: #17 (таймбокс `run_create_set_postprocess`) — покрыл 1 из ~4 операций фазы
  финализации, watchdog-слепое-пятно done>=total осталось → фриз в promo/build_response/DB висел вечно.

### CONTENT_REUSE_ACCOUNT_PASS — account-level reuse контента по ct/brand (перф, #16, НЕ баг, 2026-07-09)
- Симптом: НЕ ошибка — оптимизация. Каждая марка/модель (ct+brand) генерилась заново в каждом наборе
  аккаунта (per-набор кэш `_generated_content_by_key` сбрасывается каждый набор) → лишние вызовы LLM,
  тормоз прохода.
- Где: `create_set_orchestrator.py` точка интеграции контента (~667 read / ~704 write);
  `ai_content.py` account-кэш.
- Механика фикса: account-level in-memory кэш `_ACCOUNT_CONTENT_CACHE` (ключ `(login, agent, site,
  city, ct, brand)`), живёт весь проход воркера. Порядок источников: набор-кэш → account-кэш →
  генерация → запись в оба. Тумблер env `DIRECT_CONTENT_REUSE_ACCOUNT` (дефолт ON).
- ⚠️ Обход кэша при recreate/repair: `_force_recreate_item = force_recreate(name, _repair_force_names)`
  → account-кэш read пропускается (дефектной РК нужен свежий контент, не старый из кэша). Grabля,
  которую избежали: reuse устаревшего контента при пересоздании — закрыт обходом.
- brand-first сохранён: ключ включает ct+brand (чужая марка не подставится). slepok-scoping: смена
  слепка = смена agent-ключа → кэш не переживает. Потокобезопасность: свой Lock account-кэша ВНЕ
  `_guard` (без вложенных локов), покрывает prefetch 3w / каналы C1.
- Статус: 🟡 код на Mac (py_compile OK; изолированный smoke helpers зелёный: roundtrip/isolation/
  scoping/TTL/toggle/size-cap). НЕ деплоено (единый рестарт после Фазы 1). **live не проверено** —
  покажет полный прогон.
- НЕ помогло ранее: — (первая реализация account-level reuse; process-global `_CONTENT_CACHE` не
  покрывал потоковый prefetch-путь, т.к. он зовёт `_cached_campaign_content(fast_mode=True)` в обход).


### K4_CONTENT_STYLE_WEAK_STATIC_RESERVES — слабые филлеры/промпты сработали на деградации M3 (D1/D7/D11, 2026-07-09)
- Симптом (блок К4, качество, не «живая» ошибка): при деградации генерации на мёртвом M3 (до
  circuit-breaker'а) в live уходили слабые СТАТИЧЕСКИЕ резервы: (D1) филлеры быстрых ссылок с висячим
  годом «Господдержка на авто 2025» и размытыми канцеляризмами «Проверим доступные программы»/«Зафиксируем
  персональные условия»; (D7) короткий аварийный default_text «Авто в кредит на выгодных условиях.
  Оставьте заявку!» (~52 симв, не добивался до 81); (D11) канцелярское «Кредит на авто …» и госпрограмма
  абсолютной суммой «Господдержка 925 000 ₽».
- Где: D1 `blueprint.py:5960 _GENERIC_SITELINK_FILLERS` + промпт `ai_agents.build_sitelinks_messages`;
  D7 `ai_agents.py:~1903` аварийные fallback titles/texts в `assemble_campaign` (эти texts — источник
  default_text ShoppingAd, `create_set_feed_builders.py:636` берёт `next(t for t in texts if len<=81)`);
  D11 промпты `ai_agents.build_titles_messages` + `build_texts_messages`.
- Root-cause: статические резервы/промпты не поднимали планку — годились как «не пусто», но не как
  «продающе». Среду чинит circuit-breaker (M3 мёртв→OpenRouter), но резервы/промпты — отдельно.
- Решение (2026-07-09, ПЕРЕДЕЛКА — прошлый агент упал на API-ошибке; правки внесены напрямую через Edit):
  - **D1:** 8 филлеров переписаны на конкретные офферы с цифрой; убраны висячий год «2025» и
    канцеляризмы. Topic-дедуп сохранён 1:1: бакеты credit×2(взнос+одобрение), gift(КАСКО), tradein,
    discount, support(господдержка), testdrive, availability = 8; credit≤2 (SITELINK_CREDIT_DUPLICATE).
    Промпт `build_sitelinks_messages` усилен запретом висячего года и размытых канцеляризмов. Проверено
    изолированно: все 8 проходят `_bad_ad_sitelink` И `_bad_sitelink_phrase`, title 22–30 / desc ≤60,
    бакеты diverse (credit=2). NB: «автокредит»/«господдержку до 20%» в сайтлинках РАЗРЕШЕНЫ — сайтлинки
    валидирует `_bad_ad_sitelink` (через `_BAD_CONTENT_RE`), а НЕ `_BAD_AD_TEXT_RE`.
  - **D7:** аварийные тексты уплотнены (~52→66–77 симв, ≤81): brand «{brand} в кредит от 9 000 ₽/мес.
    Первый взнос 0 ₽. Быстрое одобрение.», non-brand «Купить авто в кредит от 9 000 ₽/мес. Первый взнос
    0 ₽. Одобрение за 30 минут.». В текстах «автокредит» НЕ используется (блок `_BAD_AD_TEXT_RE`) →
    «в кредит». Аварийные заголовки: brand-first «{brand} в кредит …», non-brand «Автокредит от 9 000
    ₽/мес. Первый взнос 0 ₽» (в заголовках «автокредит» разрешён). Проверено: titles ≤56 (43–49),
    texts ≤81 (66–77), `_bad_ad_title`/`_bad_ad_text` = False.
  - **D11:** в промпты titles+texts добавлены правила стиля: (1) «Кредит на авто»→«Автокредит» ТОЛЬКО в
    заголовках (в текстах «автокредит» блокируется `_BAD_AD_TEXT_RE` → там «в кредит», естественные
    формулировки); (2) господрограмма/выгода в % а не абсолютным ₽ (₽ только для платежа «от N ₽/мес»);
    (3) CTA «Купить … в кредит / по госпрограмме / оставьте заявку». brand-first (§2.1) НЕ тронут (правило
    подлежащего-марки на месте), длина ≥48 (§2.2), дедуп УТП (§2.3) на месте.
- Статус: 🟡 код на Mac (py_compile OK; изолированный smoke: 8 филлеров валидны+бакеты, emergency
  titles≤56/texts≤81 валидны, «автокредит» отсутствует в emergency-текстах). НЕ деплоено (единый рестарт
  после Фазы 1). **live не проверено** — проверит re-run генерации.
- НЕ помогло ранее: — (первая правка этих резервов; ключевая грабля — «автокредит» в текстах отвергается
  `_BAD_AD_TEXT_RE`, поэтому автокредит-правило только для заголовков; в сайтлинках — можно).

### K2_AUDIT_COVERAGE_HOLES — петля добивки давала ложное «чисто» на 7 классах контент-дефектов (2026-07-09)
- Симптом (блок К2, дыры покрытия, не «живая» ошибка а пробел петли): два аудита — система #1
  `grid_content_verifier` (структура) и система #2 `campaign_spec_audit.audit_campaign` (качество) —
  НЕ ловили 7 классов дефектов → карточка «чисто», а в кабинете дефект. Примеры: tp5 712608932 группа
  ct0010-«Дром» получила ключи «ситирей»; tp5 без brand-first/коротких-заголовков аудита; UAC видео-марки
  без видео; объявления с <3 текстами; tp5 «Общее» 712608932 групп меньше слепка; tp2 712606684 без
  глоб.минуса на кампании; брендовый ct-tp1 берёт картинки только из общего пула.
- Где: `campaign_spec_audit.py` (детекторы+фиксеры), `repair_planner.py` (роутинг),
  `blueprint._run_spec_audit_and_fix` + `_spec_audit_deps` (wiring), `create_set_master_product.py:423`
  (create-side), `text_gen.py` (дискриминаторы), `kontent_pack.py` (манифест-чек), `grid_finalize.py`
  (in-place минус).
- Root-cause по пунктам:
  - **D8:** `_audit_search_keywords` гейты (стр. ~259/325) резали FOREIGN_MODEL_KEYWORDS/KEYWORDS_WRONG_GROUP
    только сегментом «Модели» → тема/«Общее»-группы не проверялись; плюс «ситирей» (кириллич. транслит
    Geely Cityray) не был в `_auto_brand_tokens` → не распознавался как модель-токен.
  - **D2:** ветка tp5 в `audit_campaign` (~1157) НЕ вызывала `_audit_tp1_adaptive`/`_audit_brand_not_first`,
    хотя tp5 несёт TextAd (7 заголовков, DoD §3.5).
  - **D3-UAC:** tp6/tp7 пост-аудит делал только feed-фильтры+короткие заголовки, VIDEO-аудита не было;
    create-side `videos_for_ct(login, c_ct)` без `brand_hint` → «Марки»-ct давал пустой видео-пул
    (feeds_ct_model[ct]=None → brand_word="").
  - **D9:** число текстов объявления (<3) никто не считал на live.
  - **D10:** `_audit_plan_vs_slepok` проверял только НАЛИЧИЕ типа, не число групп.
  - **D6:** глоб.минус на кампании (1.4) был вынесен в P1-отложенное (нужен shared-set-id).
  - **D5:** «брендовый ct берёт картинки из общего пула вместо своей ct» никто не ловил.
- Решение (2026-07-09):
  - **D8:** гейты сняты до `("Модели","Общее")`; FOREIGN_MODEL_KEYWORDS для тема-группы = любой ключ с
    токеном из `_auto_brand_tokens` (word-boundary); «ситирей»/«кулрей» добавлены в
    `_AUTO_BRAND_CYRILLIC_EXTRA`. Фиксер `fix_foreign_model_keywords` (удаляет `foreign_kws` по фразе)
    работает generic. Fail-safe: набор токенов пуст → не флагаем.
  - **D2:** tp5-ветка вызывает `_audit_tp1_adaptive`(BUTTON_MISSING+SHORT_TITLES, `groups=None`) +
    `_audit_brand_not_first`. ShoppingAd/ListingAd (без titles) не трогаются. Фиксеры уже подключены.
  - **D3-UAC:** новый `_audit_uac_video_missing` (`UAC_VIDEO_MISSING`) → добавлен в `_RECREATE_CODES`
    (существующий UAC recreate). Fail-safe: не видео-марка / медиа-блок не прочитан (images==0 И
    content==0) / videos>0 / пул пуст → []. Create-side: `videos_for_ct(login, c_ct, brand_hint=c_brand)`.
  - **D9:** `CONTENT_TEXTS_LOW` в `_audit_tp1_adaptive` (читает `bodies`; fail-safe `bodies is None`→skip);
    фиксер `fix_texts_low` = `_regen_texts` + Grid RMW `_grid_update_adaptive_ads` (bodies; RMW сохраняет
    titles/images/video/цену). Wired в `_run_spec_audit_and_fix` + planner `content_texts_repair`.
  - **D10:** `_audit_group_count_vs_slepok` (агрегатное покрытие модель-ct по аккаунту vs `_struct_cts`)
    — **report-only warn**, БЕЗ авто-фиксера (детект без ремонта зациклил бы reschedule «до нуля»,
    журнал I). Осознанно агрегатно (не per-сегмент — segment-per-campaign надёжно не маппится).
  - **D6:** `_audit_global_minus_campaign` (tp2/tp4/tp5, читает inline `minusKeywords`+
    `libraryMinusKeywordsIds`; fail-safe: shared-set есть → молчим) → `fix_global_minus_campaign` →
    `grid_finalize.set_campaign_minus_keywords` (inline UpdateCampaigns, БЕЗ баллов, идемпотентно).
    Детект и ремонт оба на inline → консистентны, без цикла.
  - **D5:** `_audit_ct_slepok_images` (`CT_SLEPOK_IMAGES_EMPTY`, **report-only warn, minimum**): брендовый
    tp1-ct с пустым `kp.has_slepok_images` (лёгкий манифест-чек БЕЗ скачивания байтов) → берёт только
    общий пул. БЕЗ авто-фиксера (наполнение слепка = контент). Fail-safe: чек упал → skip.
- Осознанные упрощения (честно): D10 агрегатно+report-only (tp5 feed-driven, brands≠slepok-ct);
  D5 = наличие ct-картинок в манифесте, а не per-image reverse-lookup живого объявления. Оба report-only
  (визуализация, не автопочинка) — чтобы детект-без-ремонта не зациклил reschedule.
- Статус: 🟡 код на Mac (py_compile OK; pyflakes чисто — 0 новых undefined-name; изолированные smoke:
  `_auto_brand_tokens` содержит «ситирей»/«кулрей», foreign-match ключа True; `_uac_video_brand`
  корректно матчит Haval/Москвич/BAIC/Belgee и отсеивает Chery/пусто; `has_slepok_images` не падает).
  НЕ деплоено (единый рестарт после Фазы 1). **live не проверено** — re-run на аккаунте с дефектами
  (psm5h7q6: tp5 «Дром»/ситирей, tp2 712606684 минус, UAC видео-марки).
- НЕ помогло ранее: детект «пустоты» по одному groups_for_edit — edit-view лаг (журнал I/J). Здесь:
  keyword-детекты по `keywords` из showConditions-запроса groups_for_edit; D10/D5 report-only (лаг даёт
  максимум лишний warn, ничего не удаляет); D6 читает кампанийный payload (не group-level).

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

### VIDEO_NO_POOL_TRANSIENT_INDEX — транзиентно пустой manifest → VIDEO_MISSING ложно становится VIDEO_NO_POOL, догрузка молча не перепланируется (2026-07-12)
- Симптом: живой видео-пул на диске на месте, но hasVideo=false объявления tp1 РСЯ не догружаются и НЕ переочередиваются; в аудите вместо `VIDEO_MISSING` (fixable) висит `VIDEO_NO_POOL` (fixable=False).
- Где: `campaign_spec_audit.py:_audit_tp1_adaptive` (классификация VIDEO_NO_POOL vs VIDEO_MISSING) → `_ct_has_pool_video` → `kontent_pack.videos_pool_for_ct` (`_load_index().external_assets`).
- Root-cause: `_load_index()` при недоступном/переписываемом `manifest.json` возвращает пустую заглушку (`external_assets={}`). Тогда `_ct_has_pool_video(ct)` даёт False для ВСЕХ ct → все объявления уходят в `video_no_pool` (fixable=False), `video_missing` пуст. Reschedule-плита завязана ТОЛЬКО на `video_missing_fix.still_missing_total` (queue_server.py:1060 `remaining += _video_still`) → нет VIDEO_MISSING = нет ретрая. Транзиент индекса замаскирован под «нет пула».
- Решение (2026-07-12): гейт свежести. Новый `kontent_pack.video_index_suspect_empty()` → True ТОЛЬКО когда видео-индекс глобально пуст (ни одного ключа `Video|video|*`) НО физический пул `_video_pool/ctNNNN/*.mp4` на локальном зеркале существует (без sshfs-обхода M3). В `_audit_tp1_adaptive` no_pool собирается на ad-level; если гейт True — ролики переводятся в `VIDEO_MISSING` (retryable) вместо финального VIDEO_NO_POOL → существующая плита `still_missing_total`→`remaining` даёт reschedule. Следующий (здоровый) цикл разложит корректно: covered→attach, genuinely-uncovered→VIDEO_NO_POOL терминально. Настоящее частичное покрытие (журнал VIDEO_NO_POOL 08.07) сюда НЕ попадает — индекс НЕпуст → гейт False.
- Статус: 🟡 код на Mac (Mutagen→LXC101), py_compile OK; ждёт живого прогона. Верификация: при пустом manifest + живом `_video_pool` аудит эмитит VIDEO_MISSING (не VIDEO_NO_POOL) и delayed-repair переочередивает; при непустом индексе поведение без изменений.
- Верификация direct_verifier (2026-07-12): ✅ код подтверждён — `kontent_pack.video_index_suspect_empty()` (1302-1335) + ad-level буфер `no_pool_ads` в `campaign_spec_audit._audit_tp1_adaptive` (888-924); не-транзиентный путь (индекс непуст) — `no_pool_ads` не используется, `VIDEO_NO_POOL` эмитится идентично pre-fix; UAC-путь не тронут; conservative fallback (`_LOCAL_MIRROR_ROOT=None`/нет `_video_pool` → False) = прежнее поведение. Остаётся 🟡: live-прогон при реальном транзиентном `manifest.json`.
- НЕ помогло ранее: — (новый путь; отдельный отказ от VIDEO_NO_POOL by-design 08.07 — там частичное покрытие, индекс НЕпуст).

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

### TP5_CODER_MISSING_MULTI_BRANCH 🟡 ждёт прогона

- **Симптом:** в tp5 «Товарная галерея - Модели» (camp_names/per-модель путь) имена групп были сырыми
  структурными метками («Jetta», «GAC Gs4»…) без кодер-префикса — нарушение правила «кодер первый».
- **Причина:** `create_set_tp1_builders.py:927` ветка `_multi=True` отдавала `_uname` напрямую как
  `group_name`, минуя `_tp1_group_name`; все остальные ветки (в т.ч. недавний non-multi tp5 фикс
  коммит `402949f7`) кодер-построитель вызывали.
- **Фикс (2026-07-22):** `create_set_tp1_builders.py:931-933` — `_uname` теперь передаётся
  как `brand` в `_tp1_group_name` (а не как готовый `group_name`); non-multi-путь не тронут.
- **НЕ помогло ранее:** — (первая правка этой сигнатуры).

---

### TP6_TP7_SILENT_SKIP_NO_WARNING 🟡 ждёт прогона

- **Симптом:** если структура слепка содержит tp6/tp7-позиции, но в `variants` UI-запроса не
  выбран соответствующий master/product-вариант — блок `_emit_struct` молча не вызывался, никакого
  предупреждения в логе job'а не появлялось. Пользователь узнавал об этом только через DOD-аудит.
- **Причина:** проверка «что в структуре» выполнялась только ВНУТРИ `_emit_struct` (и только на
  пустоту) — условие `want_master/want_product` было гейтом без обратной связи.
- **Фикс (2026-07-22):** `create_set_plan.py:1291-1305` — перед вызовами `_emit_struct` добавлены
  явные checks: если `not want_master` и `_slepok_struct_groups(...,"tp6")` непустой → warning в
  `warnings` плана; аналогично для tp7. НЕ блокирует, НЕ меняет логику создания.
- **НЕ помогло ранее:** — (первая правка этой сигнатуры).

---


---

## Решённые ранее и история прогонов → [ERRORS_JOURNAL_ARCHIVE.md](ERRORS_JOURNAL_ARCHIVE.md)

Компактная таблица ✅-закрытых сигнатур (`DUPLICATE_SITELINK_DESCS`, `IMAGE_NOT_FOUND`,
`FEED_NOT_EXIST`, `UAC_400_sitelinks`, ложный `UAC_PRODUCT_MODEL_FILTER_MISSING`,
ложный `SITELINK_MISSING`, пустые черновики, дубли джобов/tp6-tp7) — там же.

Разбор прогона **2026-07-06** (A–K sub-записи):
`A:✅ MUST_NOT_CONTAIN_DUPLICATED_ELEMENTS` · `B:✅ INVALID_COLLECTION_SIZE` · `C:✅ tp5 без ShoppingAd` ·
`D:✅ AddUnifiedAdGroups CAMPAIGN_NOT_FOUND` · `E:🟡 NO_BRAND_SEGMENTS_AVAILABLE` ·
`F:✅ job stuck in claimed` (НЕ помогло: первый watchdog-вариант) ·
`G:🟡 приоритет доделки` · `H:бэклог updateListingAds UNAVAILABLE_FIELD` ·
`I:✅ ложный GENERIC_FALLBACK_GROUP` (НЕ помогло: single-source groups_for_edit) ·
`J:✅ Grid finalize startDate КАРУСЕЛЬ` (НЕ помогло: fix только read-builders; **HAR-шаблоны с датами = бомба** — вынесено в AGENTS.md) ·
`K:🟡 INTERRUPTED_JOB_POSITIONS_LOST`

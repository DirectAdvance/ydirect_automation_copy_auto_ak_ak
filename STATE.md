# Нейродиректолог — Состояние

> Читать ПЕРВЫМ в начале каждой сессии. Обновлять ПОСЛЕДНИМ перед выходом.
> Ошибки создания РК: сигнатуры/решения/что-помогло — **ERRORS_JOURNAL.md** (обязателен к заполнению при фиксах).

> Архив сессий старше 3 дней — **STATE_ARCHIVE.md** (перенесено 2026-07-13, 230 секций / 7215 строк). Правило ротации — см. CLAUDE.md.

## Сессия 2026-07-13 — Фикс check_blocks 500 (pull-lock не сконфигурен в content-процессе, ЗАДЕПЛОЕНО+верифицировано)
«Проверить блокировку» под АДМИН-сессией кидала `SyntaxError: Unexpected token '<'` = 500 HTML. ВТОРОЙ, независимый корень (не тот же, что stale-Victory-conn): `account_service.py:25` держит `_pull_begin=_pull_end=_busy_response=_missing`; реальные реализации инжектятся только через `automation_runtime.configure()` — wiring процесса direct-CREATE. Процесс direct-CONTENT (`content_main.py`) pull-lock не конфигурит → `_pull_begin` навсегда `_missing` → 500. Бил именно admin/full-access (кто минует `_pairs_allowed`→DB). **Фикс (Вариант Б, scoped, только `account_service.py`):** самодостаточные дефолты pull-lock прямо в модуле (`import threading,time` + `_PULL_LOCK`/`_PULL_LAST`/`_PULL_OWNER` + `_default_pull_begin/_end/busy_response`, зеркало `automation_runtime` :327/:341/:592); строка 25 = дефолты вместо `_missing`. direct-create по-прежнему перезаписывает их своим `configure()` (единый лок движка) → его поведение НЕ меняется. `content_main.py` не тронут (чистая граница, движок не импортируется). Прочие `_missing`-деп-ы (delete/assets/stopall) оставлены — их эндпоинты в content-процессе не регистрируются (только balance+check_blocks). Деплой LXC101 (md5 сверен, py_compile+pyflakes чисто, рестарт direct-content, active PID 1908114; content_worker account_service не импортит — не рестартили). Верификация test_client форс-admin на боевом коде: check_blocks #1 → 200 JSON `{"blocks":{"porg-asfbs7qe":false}}`; повтор <60c → 429 JSON cooldown; balance регресс 200; журнал до рестарта 8× RuntimeError+500, после — 0. Детали — ERRORS_JOURNAL `CHECK_BLOCKS_PULL_LOCK_MISSING_500`.

## Сессия 2026-07-13 — Фикс content-editor 500 «not valid JSON» (stale Victory conn, ЗАДЕПЛОЕНО+верифицировано)
Content-editor ронял 500→HTML («Unexpected token '<' … not valid JSON») на «Обзоре»/балансе/«Проверке блокировок» после простоя. Корень: `direct_repository.py::victory_conn` проверял только `conn.closed`, а Victory Postgres молча роняет idle-conn (SSL EOF) при `closed==0` → первый I/O `set_session` кидал `OperationalError: SSL SYSCALL error: EOF detected`. **Фикс (scoped, только `victory_conn`):** пинг `SELECT 1` в try/except OperationalError → мёртвый сокет `putconn(close=True)` + ОДИН ретрай на свежем conn, иначе проброс. Здоровый путь не изменён. Задеплоено на LXC101 (md5 сверен, py_compile+pyflakes чисто, рестарт direct-content+worker, оба active). Верификация: accounts?status=__all__ 200 JSON 1238 rows; симуляция реального EOF (`pg_terminate_backend` СВОЕЙ сессии, closed остался 0) → прозрачный reopen (RECOVERED), 500 нет; unauth 401; журнал чист. **check_blocks — тот же корень** (её DB-контакт только через `_pairs_allowed()→victory_conn()` для scoped-юзера; ядро блокировок cookie/Grid без БД) → отдельно не чинил. Детали — ERRORS_JOURNAL `VICTORY_CONN_STALE_SSL_EOF_500`.

## Сессия 2026-07-13 — «Сверка цен» доступна обычным юзерам со скоупом (ЗАДЕПЛОЕНО, верифицировано)
Content-editor (`direct-content.service` :5021). Вкладка «Сверка цен» была admin-only (`_admin_allowed()` на каждой ручке) → сделана доступной всем с моделью доступа 1:1 как «Обзор»: full access (is_admin OR content_admin) видят всё, обычный юзер — только своих директологов из `content_editor_access.json` (в `.secret/`).
- **price_check.py:** `logins_for`/`active_logins`/`diff_rows` получили `allowed: list[str]|None` (None=все, []=пусто, иначе `directologist=ANY(%s)`); `jobs_recent`/`apply_queue_for_ui` — `created_by` фильтр; новый `job_created_by(job_id)`.
- **routes_content_editor.py (2513-2698):** убран жёсткий `_admin_allowed()` на run/status/jobs/results/apply/queue/pause/resume/delete/start_now. Хелперы `_pc_scope_deny()` (мутации: []→403) и `_pc_job_owned(job_id)` (не-админ управляет ТОЛЬКО своими джобами по created_by). results/logins скоупятся по `_allowed_directologists()`; apply — `_login_allowed_for` на каждый item (чужой login→403); status/pause/resume/delete/start_now — ownership. `ce_admin_directologists` тоже открыт со скоупом (дропдаун). `queue/cleanup` остался admin-only (глобальная чистка). balance/check_blocks уже были scoped (`_pairs_allowed`).
- **content_editor.html (~3125):** вкладка `ce-pricecheck-tab` показывается ВСЕМ; «Пересчитать сейчас» + queue-cleanup остались is_admin.
- **Верификация (LXC101, реальная БД+test_client):** func-level (diff_rows/logins_for) + route-level 11 кейсов PASS: admin видит все дир-ги; karavaev видит только Караваев (5000 строк, 0 утечки); чужой directologist/login в query→0; run чужого→404; apply чужого→403; status/pause/resume/delete/start_now чужой job→404/403; admin тот же job→200; no-access→пусто+403; регрессии на Обзоре нет (ce_accounts karavaev=22 акка только Караваев). Деплой: рестарт direct-content+worker (active), page 302, API noauth 401, лог чист.

## Сессия 2026-07-13 (вечер) — Разбор 10 ошибок прогона porg-asfbs7qe + UI-бейдж таргетинга + деплой (ЗАДЕПЛОЕНО)
Параллельный разбор всего списка ошибок движка + слепки. **5 код-фиксов ЗАДЕПЛОЕНЫ на LXC101** (md5 сверен, py_compile OK, рестарт direct-create+worker, smoke 302, direct_verifier ✅ по 5 пунктам). Окружение было чисто (0 активных джоб — worker-рестарт безопасен).

**Задеплоенные фиксы (все 🟡→на проде, ждут живого прогона):**
- **#1 FALSE_152_TOKEN_RETRY_TECH_FAIL** (`create_set_orchestrator.py` ~1190/1223): куки-remainder гейт ужесточён — кука ТОЛЬКО при `_units_dead_confirmed=True` ИЛИ `_rc>=_RESUME_MAX`; техпадение token-retry при живых баллах больше НЕ пишет ложное «баллы исчерпаны» и не флипает на куку. Родит. строка → waiting (не теряем пункты).
- **#3 CPA_BUDGET_RULES_IGNORED** (`create_set_plan.py:318`): `_bud("cpa")` = `rs["budget"]` (было `rs["cpa"]*10`). CPC не тронут.
- **#6 CREDIT_IN_DEFAULT_TEXT_PRODUCT_TP3** (`create_set_feed_builders.py` ~982): tp3 ShoppingAd default_text = `SHOPPING_DEFAULT_TEXT` (был credit-angled slepok-текст; tp5 уже был фикшен R2-8, tp3 пропустили). Семён: «писать изначально общий текст».
- **#9 SLEPOK_MINUS не долетал** (`create_set_minus.py` ~95/160): паковый `_minus_shared` применялся только в group-режиме; теперь и в campaign (inline) + shared_set (library). ✅ Остаточный гэп ЗАКРЫТ (2026-07-13 доп.фикс, НЕ задеплоен): реальный незакрытый путь был не token (там `_apply_campaign_direct_minus` step5 уже кладёт inline даже при reuse Grid-набора), а **cookie** `_create_text_via_cookie` (tp2/tp4) — его `spec.minusKeywords` нёс только глоб. слова. Фикс: `_mk_words`=глоб+`_collect_pack_minus` (гейт mode!=group & tp!=tp1, кап 20k) INLINE в spec per-кампания; расшаренный аккаунтный набор НЕ мутируется. Deps добавлены в `_create_set_feed_builder_deps`. py_compile+pyflakes+offline OK, ждёт живого прогона (pavlov/kryuchkova/scherbakova via_cookie).
- **#8-audit VIDEO_NO_POOL_AUDIT_IGNORES_SLEPOK_POOL** (`campaign_spec_audit.py` ~804/1321): аудит видео был pool-only, теперь слепок-aware (`videos_for_ct`/`videos_for_login`) — симметрично создатию. Создание РК видео УЖЕ было слепок-aware (не баг). Для porg-asfbs7qe видео реально нет ни в общем, ни в слепковом пуле (марки autodealer-nsk).

**Структура слепков (ЗАДЕПЛОЕНО, `slepki_structure.json`, backup editbak.20260713_180416):**
- chepelev tp4 УДАЛЁН (Моно+Мульти, всё ARCHIVED/0 ON); kryuchkova Квиз/tp7 ДОБАВЛЕН; scherbakova tp1 Модели-КС `gc` ct0014→ct0001 (была коллизия с Марки-КС).
- **UI-бейдж таргетинга #10 ЗАДЕПЛОЕН:** поле `t['tgt']` запечено на 76 узлов tp1-5 из `reconciler_staging/verify_tp15_*.json` (мода метки); фронт `templates/direct/index.html` (`_tgtFromBaked`+`_TGT_BADGE_MAP` ~1480/1489, `slepkiTpTree` ~1610) показывает живую метку (КС/КС+авто/ауд+авто/КС+ауд+авто/автотаргетинг), gc-fallback где нет данных. tp6/7 — из имени (не трогали). ⬜ Визуальный рендер в браузере Семён смотрит сам.

**Разобрано, НЕ фикшено (ждёт решения Семёна):**
- **#4 NAME_FEED_NOT_IN_CAMPAIGN**: в коде расхождения имя↔feed_id НЕТ (оба из одного plan-item). Почти наверняка дубль `TP7_FEED_FANOUT` (пустая бренд-кампания). Live-факт снять не с чего (черновики порг пересозданы, tp7 нет). → закрыть как дубль ИЛИ 1 tp7-прогон через слепок с Метрикой.
- **#7 HEADLINE_CHAR_BUDGET_UNDERUSE**: root-cause ПОЛНЫЙ — `_fill_title` (`text_gen.py:988`) хвост `_TITLE_TAILS`(6-8)+«. »(2)=+8 не влезает в базу 49-53 (>56) → застревает 48-52. Гейты поднимать НЕЛЬЗЯ (схлопнется набор+brand-first). Фикс = переписать шаблоны-ИСТОЧНИКИ (`_brand_title_set` text_gen.py:1022, variants `_upgrade_credit_titles` create_set_assets.py:325) чтобы сами были 53-56. **Зона direct_copywriter, нужен вкус Семёна.** (codex-look советовал поднять lo=45→53 — МЁРТВАЯ правка, lo не читается циклом.)
- **#8 видео-данные**: наполнить `_video_pool/<ct>` или слепковый `_slepki_data/<slepok>/videos/` для марок autodealer-nsk, ИЛИ закрыть «у марок видео нет».
- **Метки tp6/7** (RESOLVED salamahin/zubakin/tumashenko/karavaev + hybrid pavlov/terehov/chepelev): данные готовы (`verify_tp15_*.json` + staging), Семён ОТЛОЖИЛ применение.
- **🆕 Щербакова С пробегом**: MISSING по реестру `local_gsheet_sites` — 2 живых аккаунта «Контекст активно», в структуре site_type НЕ заведён. Нужен live-харвест (отдельно, лок-риск).
- **scherbakova B1 tp7**: 3 узла с одинаковым gc — НЕ баг (live: gc физически один, различие по targeting_mode; UI-бейдж #10 покрывает). B2 (Модели-КС ct0001) применён.

**Закрыто окончательно:** tumashenko Мульти+БУ — реестр `local_gsheet_sites` подтвердил: 2 логина, ОБА «Удален» → собирать нечего, не добирать. gordeeva 28-tp7 vs tp6 — codex: 28 не эталон (staging=2 generic), tp6 добирать по distinct live-позициям, НЕ bulk. Постпроцесс #4-9 — existence-верификатор, семантики контента нет by design (не «сломан»).

⬜ **ОСТАЛОСЬ (доки):** занести UI-бейдж `tgt`-механику в ARCHITECTURE.md/CONTENT_EDITOR.md; отразить B2/presence-правила. ERRORS_JOURNAL уже дописан по каждому фиксу.

## Сессия 2026-07-13 (поздний вечер) — UI-панель таргетингов группы + URL-фикс модели + правило tp2-5 (ЗАДЕПЛОЕНО)
Продолжение вечерней сессии по уточнениям Семёна. Второй деплой (рестарт direct-create+worker 20:33, smoke 302, 0 активных джоб).
- **Правая панель таргетингов группы (index.html, ЗАДЕПЛОЕНО):** в режиме редактирования кнопка «👁 таргетинги» (в `_slItemBtns` ~1464, рядом с 🔑) → `slepkiOpenGroupDetail(gc,tp,name)` рендерит СПРАВА (`#slepki-detail`, flex рядом с `#slepki-tree` ~1179) столбец таргетингов группы. Данные = ПАК через существующий роут `GET /direct/api/slepki/keywords` (backend 0 правок). КС/гибрид → список ключей + маркер autotargeting + свёрнутые минусы; чистый автотаргет (пак пуст, `_aon_`) → «Автотаргетинг — ключей нет»; аудиторный (ct0002-0005) → «Аудитория: <тип>». Backup index.html.editbak.20260713_185936.
- **URL-фикс модели (ЗАДЕПЛОЕНО):** см. ERRORS_JOURNAL `MODEL_URL_BRAND_FALLBACK_WRONG_MODEL`. Вариант A (`no_brand_fallback` для «Модели» в `create_set_feeds.py:_feed_url_for_model` + 3 call-sites). UNI-T больше не идёт на чужой cs55 → формульный `/auto/changan/uni-t`. ⬜ Вариант B (точный url из фида `/i/suv-5d` через mark_id/folder_id из Grid listings/raw XML, т.к. FeedOffersPreview = sample и не содержит все офферы) — ОТЛОЖЕН по решению Семёна (вариант A выбран).
- **Правило tp2-5 «КС+авто» (ЗАДЕПЛОЕНО):** `verify_tp15_pull.py:_label` — для tp2-tp5 при `real_keywords>0` метка форсит автотаргетинг-компонент (КС→КС+авто). Пересчёт существующих verify_tp15: 0 изменений (данные УЖЕ конформны через `---autotargeting`-маркеры в кабинетах). Правка = forward-robustness. Фронт/структура не менялись (tgt уже «КС+авто», карта знает метку).
- **⬜ ОТКРЫТО (per-группа гранулярность бейджа):** verify_tp15 хранит метку per-КАМПАНИЯ (агрегат), НЕ per-структурная-группа. Для метки per-марка (Москвич отдельно) нужен маппинг живой adgroup→ct + доработка сбора (codex-look в фоне разбирал). Правая ПАНЕЛЬ (детали по клику) — из пака, работает; per-группа БЕЙДЖ (метка на каждой марке) — отдельная задача.

## Сессия 2026-07-13 — Правила tp6/tp7: метки таргетинга, кодер aon+ct001/ct010, UI-бейдж (документация)
- **Что сделано:** обновлена документация под согласованные с Семёном правила tp6/tp7. ТОЛЬКО .md-файлы, код/JSON не тронуты.
- **CODER.md:** ag_part2 — пометка «tp6/tp7 всегда aon»; ag_part5 — новая строка UAC ТК (ct010+ag001), убрана tp7 из строки ct009 (она только tp3); секция «tp6/tp7 UAC кодер» — полностью переписана: формат имени `{базовое имя} - {метка таргетинга}` без CPC/CPA, правило метки из факта (ключи/аудитории), UI-бейдж, дедуп стратегий, позиция-кодер gc; пример фидов — ct009→ct010 для tp7, имя без cpc.
- **DOD.md §3.6/3.7:** добавлены `- [ ]` пункты: имя кампании с меткой таргетинга, кодер позиции gc.
- **DOD.md §5.d:** добавлены правило «метка из факта» (с уроком по «hybrid»-инциденту) и «Квиз/Неопределено — вне зоны сбора слепка».
- **ARCHITECTURE.md:** tp6/tp7 строки таблицы tp-типов — добавлены кодеры и формат имени; раздел «Позиция слепка» — добавлены правило метки + UI-бейдж + дедуп стратегий.

## Сессия 2026-07-13 — Редактор ключей слепков: 2 колонки + редактируемые библиотечные минусы (готово, задеплоено)
- **Задача 1 (вёрстка):** в раскрытом редакторе ключей (Структура слепков → Редактирование ВКЛ → «Ключи · …») positive и минусы стояли стопкой. Сделал 2 колонки: слева positive, справа стопка [per-slepok _minus.txt + библиотечные _minus_shared.txt]. Grid `.sled-kw-cols` (1fr 1fr, `@media max-width:720px`→1 колонка). Кнопки под колонками. `templates/direct/index.html` (CSS ~1132, JS `slepkiOpenKeywords`/`slepkiSaveKeywords` ~1613-1650).
- **Задача 2 (библиотечные минусы, scoped-editable):** раньше `minus_shared` был read-only. Теперь редактируется, SCOPE строго (slepok, site_type, tp, ct). Backend `apply_edit_keywords` (`slepki_editor.py`) пишет `{slepok}_minus_shared.txt` ТОЛЬКО если ключ `minus_shared` в spec (иначе файл не трогаем — обратная совместимость). Route `routes_slepki_edit.py` прокидывает `minus_shared` лишь при наличии в body.
- **Модель minus_shared (выяснено):** НЕ глобальный. Физически `{slepok}_minus_shared.txt` в keywords-папке каждого ct, префикс `{slepok}_` → чужие слепки физически недостижимы. Байт-идентичен по всем ct одного (slepok,site_type,tp) (md5 совпадает) = одна библиотека на (slepok,site_type,tp), размноженная по ct. Намеренно НЕ размножаю правку на соседние ct (риск клоббера ct-вариаций + тяжёлый fan-out) — пишу ровно в редактируемый ct. Broad-propagation (на все ct tp) — отдельная операция, требует явного согласия Семёна.
- **Верификация (live LXC101):** read terehov/С пробегом/tp5/ct0000 → minus_shared=1112 видны. Синтетический write на throwaway-слепок `smoketest_xyz`: has_shared ветка minus_shared_rows=2 (dedup ок), no-key ветка → None (файл не тронут). terehov md5 UNCHANGED = изоляция доказана. Junk почищен на m3-relay. py_compile+pyflakes clean, direct-create+worker active, 0 ошибок в логе.
- **Грабля (важно):** `kp.PACK_ROOT=/opt/neuro_kontent` — это READ-ONLY sshfs mount m3-relay. DST-запись `_dual_write_pack_file` в него ПАДАЕТ (read-only); реально файлы создаёт M3-ветка (`_ssh_write_m3` на m3-relay), а sshfs-mount их отражает. Не мой баг — общая инфра для всех правок ключей (то же у `_minus.txt`). Docstring slepki_editor говорит DST=/opt/neuro_content_local, но фактически PACK_MOUNT=/opt/neuro_kontent (ro). Стоит уточнить у Семёна, работает ли DST-half dual-write вообще.

## Сессия 2026-07-13 — Фикс рассинхрона display-имён слепков в двух дропдаунах (готово, ждёт рестарт)
- **Баг:** дропдаун «Структура слепков» (slepki-dir ← /api/ui_structure ← slepki_structure.json `directologists[].name`) показывал `kuderko="Кудерко Семён"`, `gen_ses="Слепок_gen_ses"`, `dmp="Слепок_dmp"`; дропдаун «Создание РК» (ac-agent ← /api/ai/agents ← ai_agents.agent_list) уже канон. Корень: 3 некорректных `name` в slepki_structure.json (display-поле, не ключ).
- **Фикс:** правка ТОЛЬКО 3 `name` в slepki_structure.json → «Слепок_Кудерко»/«Слепок_ГенСес»/«Слепок_ДМП». Ключи (kuderko/gen_ses/dmp) и структура не тронуты; `name` — display-only, все lookups по `key`.
- **Верификация:** json.load OK; оба источника (agent_list vs directologists[].name) сверены по всем общим ключам — mismatches NONE. Рестарт сервиса — за главной сессией.

## Сессия 2026-07-12 — Фикс SINGLE_FEED_TP5_TP3_WRONG_FEED (правка кода, ждёт прогона)
- **Баг:** при single_feed на аккаунте БЕЗ `/yandex.xml` tp5/tp3 создавались на первом разрешённом фиде (porg-asfbs7qe → `credit-page-01-a.xml`, лендинг), вразрез с plan/tp7 (те резолвят /yandex.xml или фолбэк). Причина: tp5/tp3 само-резолвят фид через `prefer_single_feed_variants(data["feeds"])`, которая при отсутствии /yandex.xml берёт `variants[:1]`.
- **Живой факт (v5 feeds.get porg-asfbs7qe/victoryagency14):** `/yandex.xml` в аккаунте НЕТ (51 фид, все `yandex-<brand>.xml`/`yandex-catalog-*.xml`). Значит варианты (a)«всегда мержить Grid» и (b)«читать UrlFeed.Url в allow-фильтре» не помогли бы — фида нет ни в v5, ни в Grid, а url уже был в кортеже. Верный фикс = альтернатива из задачи.
- **Правка (`create_set_feed_builders.py`):** helper `_resolve_single_feed_variants` — резолв как plan: `_first_url_feed(strict=True)` для /yandex.xml → фолбэк `yandex-catalog-model-design-custom-name.xml` только при `job.body.single_feed_fallback` → выбор из data["feeds"]; не найден → feeds=[] (tp5/tp3 пропускается, а не создаётся на чужом фиде). `_first_url_feed` добавлен в feed_builder_deps (`automation_runtime.py`).
- **Верификация:** py_compile OK; offline-симуляция на живых данных: OLD→credit-page(3505256), NEW→fallback 3505268 при confirm / [] без. ERRORS_JOURNAL обновлён (🟡).
- **Осталось:** деплой (рестарт direct-create + direct-create-worker), живой прогон single_feed на porg-asfbs7qe.

## Сессия 2026-07-12 — Харвест пака zubakin/gordeeva Монобренд (0 правок кода)

- **Задача:** 16/18 позиций zubakin и 4/6 gordeeva упали с «пак пуст» при тестовом прогоне на porg-asfbs7qe.
- **Диагностика:** `gather("zubakin", "Монобренд", "tp1")` возвращал только ct0000 (198 kw) → segment-фильтр "Марки"/"Модели" давал 0 групп → "нет ct-папок с ключами слепка zubakin". Причина: zubakin.txt/gordeeva.txt не существовали в brand-специфичных ct-папках.
- **Источники (API victoryagency14/victorylotsofads1/victoryagency-direct1618440):**
  - zubakin: 6 аккаунтов × TEXT_CAMPAIGN (всего 84 кампании, 54,024 raw kw). porg-o5x73pkx (kuban-belgee.ru, Контекст активно) — 0 кампаний (пустой счёт).
  - gordeeva: 9 аккаунтов × TEXT_CAMPAIGN (65 кампаний, 51,415 raw kw).
- **Записано на LXC101 (`/opt/neuro_content_local/kontent_oktyabr/Монобренд/`):**
  - zubakin.txt: 33 ct × 3 tp (tp1/tp2/tp5). Марки: ct0029(Changan 453kw), ct0044(Chery 210kw), ct0097(Geely 173kw), ct0181(Lada 81kw). Модели: 27+ cts.
  - gordeeva.txt: 33 ct × 3 tp (новые + слияние с существующими). Марки: ct0029, ct0044, ct0097, ct0111(Haval 712kw), ct0181. Модели: 19 cts.
  - Итого: 62,631 операций записи.
- **Probe после:** gather() zubakin Монобренд tp1 = 33 ct; gordeeva tp2 = 31 ct. Сегментирование: zubakin Марки=4, Модели=27; gordeeva Марки=5, Модели=19.
- **NO_BRAND_SEGMENTS_AVAILABLE (tp5) — вердикт:** `create_set_gallery.py:75, if via_cookie or not st_token:` → это GENERIC-заглушка, не реальный Yandex 152. Появляется ВСЕГДА при cookie-пути с сегментным tp5. «error 152» в тексте ошибки — введение в заблуждение: настоящего API-вызова нет. Retry с `_resume_via_token=True` создаётся автоматически и должен сработать с API-токеном (38M баллов в запасе).
- **Дырки остались:** ct0026/ct0027/ct0028/ct0306 (Belgee) — у zubakin нет; ct0055/ct0189 — keyword-classifier не поймал; ct0315 — неизвестный ct. Эти cts в структуре есть, в паке zubakin — нет. Группы для них не создадутся.
- **Что нужно координатору:** tp2-deferred уйдут в авто-ретрай (defer=True). tp1 "пак пуст" с defer=False (кроме M3-glitch): нужно вручную пере-запустить failing items после сессии, или дождаться следующего полного прогона.

## Сессия 2026-07-12 — Prep-шаг 1: рестарт digest + разведка porg-asfbs7qe (0 правок кода)
- **Рестарт digest.service:** прямой ssh 192.168.0.202 таймаут → через `lxc101-ts` (Tailscale) OK.
  active(running), новый PID 1013933 (был 4068513), старт 12:18:24. Лог чист (Flask :5010, без ошибок).
  Smoke `/direct/automation` → 302 (редирект на логин), `/` → 302. 404 `create_set_status?job_id=e4e59163f044` в логе = зависший поллинг старой вкладки, не ошибка.
- **Разведка porg-asfbs7qe (Павлов, autodealer-nsk) — ТОЛЬКО чтение, baseline ДО серии из 11:**
  - Агентство: **victoryagency14** (token_for_login резолвит, живой).
  - **Метрика 109986153 УЖЕ привязана**: public.metrika_goals[porg-asfbs7qe] counter=[109986153], all_forms goal=**571275138**, не расшарена на др.аккаунты → авто-подхват, tp6/tp7 CPA возможны (в отличие от porg-vfdnaolu без счётчика).
  - **51 фид** (все autodealer-nsk.ru, SourceType=URL, в осн. DONE). tp7-черновики используют **3505268** `yandex-catalog-model-design-custom-name` (197 items/198 listings). Ещё есть 3553704 `Товары с сайта` (326), и 3505264 `target` = Status ERROR (битый). ⚠️ v5 `feeds.get` НЕ отдаёт поле Url (валидные: Id,Name,BusinessType,SourceType,FilterSchema,UpdatedAt,CampaignIds,NumberOfItems,NumberOfListings,Status,TitleAndTextSources,Fields). CampaignIds пусто у всех → фиды к кампаниям пока не привязаны, но в аккаунте есть.
  - **Baseline кампаний: 21 черновик от прошлого прогона** (все DRAFT, не архив, ничего State=ON): v5 видит 8 TEXT (tp1 РСЯ ×4 + tp2 Поиск ×3 + tp1 Марки-автотаргет); Grid добавляет 13 UAC = 10 tp6 Мастер + 3 tp7 Товарка. Их надо чистить перед 1-м прогоном.
  - **Квота units: rest=38 264 610 / limit=39 400 000** (spent 11). При UNITS_PER_CAMPAIGN=2500 — с огромным запасом на всю серию (152 по units НЕ ожидается; сброс в полночь МСК). Cookie-Grid путь units вообще не тратит.

## Сессия 2026-07-11 — Харвест пака (kuderko/salamahin/terehov/scherbakova) ЗАВЕРШЁН

- **СДЕЛАНО на LXC101 (`/opt/neuro_content_local/kontent_oktyabr/`):**
  - kuderko/С пробегом/tp1+tp2: 4878 kw → 62 brand-ct папки (ct0181, ct0164, ct0121, ct0199, ct0209 и др.). Probe: pack=12 fb=0 kw=58536. **kuderko РАЗБЛОКИРОВАН.**
  - terehov: 3864 КС ключей (реальные, без «---autotargeting» маркеров) → ct0000/ct0009/ct0010 для Мультибренд/Монобренд/С пробегом × tp2 и tp4. Probe: Мультибренд/tp2 pack=11 fb=1.
- **ВЫВОД ПО СЛЕПКАМ:**
  - kuderko = ВСЕ aon. Мультибренд — api_4001 (чужое агентство), пак пуст. Структурных изменений не нужно.
  - salamahin = подтверждён aon (5 фраз incl «---autotargeting»). Пак Мультибренд полный (58867 kw).
  - terehov = реальных КС-аккаунты + структура aon. Пак заполнен. Рекомендация aon→aoff если нужен КС-режим — решение Семёна.
  - scherbakova = 212 «пустых» ct NOT её ct (это ct других слепков). Пак полный (103/103).
- **АРТЕФАКТЫ:** `scratchpad/pack_autotarget_patch.json` — структурные рекомендации.
- **ОСТАЛОСЬ:** Семён применяет patch.json к slepki_structure.json по необходимости. kuderko Мультибренд — без данных.

## Сессия 2026-07-11 — DMP_FULL_B2B_PIPELINE fix (ЗАВЕРШЁН, код на Mac, НЕ деплоено)

- **СДЕЛАНО:** все 7 точек авто-кредитного кровотечения в dmp устранены.
  - DB: 25 строк для site_type='dmp' добавлены в `public.direct_ad_templates` (12 title, 5 text, 8 sitelink).
  - `ai_agents.py`: AGENT_ADS['dmp'] sitelinks, sitelink_bank_for dmp-branch, _sitelink_bucket_limits dmp-branch,
    assemble_campaign (_is_dmp + dmp-aware _title_ok/_text_ok), build_texts_messages dmp-guard, build_sitelinks_messages dmp-guard.
  - `create_content.py`: _is_dmp + _DMP_B2B_UTP_RE, _title_ok/_text_ok + _accept_title/_accept_text dmp-branch,
    _final_fill_campaign_content dmp-fillers (12+5+8 B2B), _credit_offer_ok_line dmp=True.
- **Верификация:** py_compile OK; pyflakes 0 undefined; filter-test 8/8 titles + 5/5 texts; DB INSERT OK rows=25.
- **ОСТАЛОСЬ:** Семён деплоит (рестарт direct-content + direct-content-worker), потом первый dmp-прогон.
- ERRORS_JOURNAL.md обновлён: `DMP_FULL_B2B_PIPELINE` (🟡 ждёт прогона).

## Сессия 2026-07-11 — БАТЧ тест-прогон porg-vfdnaolu (шаг 2, ЗАВЕРШЁН, 0 правок кода проекта)
- **ФИНАЛ: 9 слепков прогнано, 68 черновиков (все DRAFT), kuderko SKIP.** Механизм cookie-Grid (via_cookie+no_cpa, без баллов) валиден.
- Создано по слепкам: pavlov 9, scherbakova 6, kryuchkova 4, gordeeva(Мультибренд) 5, zubakin 2, chepelev 9,
  tumashenko 9, karavaev(Мультибренд) 10, salamahin 6, terehov 8. kuderko SKIP (пак пуст в обоих типах).
- **Site_type по полноте пака** (правило координатора): gordeeva/karavaev → Мультибренд (С пробегом пуст);
  остальные по probe. **kuderko — единственный полный блокер: залить М3-пак** (пусто в С пробегом И Мультибренд).
- **Флаги (все известные, не стоп):** tp5-сегменты→NO_BRAND_SEGMENTS(докрутка токеном); tp3/tp5-Фиды→нет URL-фида;
  NO_IMAGES_LIVE(tp1, систематичен, auto-repair grid); NO_KEYWORDS_LIVE(tp2); NO_ADPRICE_LIVE(warn);
  M3-partial (salamahin/terehov tp2/tp4 «Общее»). tp6/tp7 не тестируемы (нет Метрики → goalId=0 HTTP400).
- **Шаг 5 (dmp B2B) — ⛔ СТОП (create упал), ТРИ блокера:** (1) пак ct0800+ ЧИТАЕТСЯ, но только с `NEURO_PACK_MOUNT=/opt/neuro_content_local`
  (env воркера, НЕ дефолт /opt/neuro_kontent — мой probe без env давал ложный kw=0; с env pack15 kw557 B2B). (2) Гейт `validate_create_set_content`
  (create_set_account.py:59): `direct_ad_templates` 0 строк для site_type='dmp' → job error «нет шаблонных текстов». (3) КЛЮЧЕВОЕ: генерация даёт
  АВТО-контент не B2B. **Попытка #2 (фикс a7b73e2 задеплоен):** direct_ad_templates dmp=25 B2B-строк ✅, ai_agents.py синкнут (mtime 09:32, воркер рестарт 09:42) ✅,
  НО превью `_cached_campaign_content(dmp)` (fast_mode True И False) ВСЁ ЕЩЁ авто-доминантно («Автокредит/Трейд-ин/КАСКО/₽мес», тексты+сайтлинки 100% авто).
  Фикс попал в fallback-таблицу+промпт, но дом-путь `_gen_campaign_content`/`_rsya_titles`/`create_set_text_builders`/`_upgrade_credit_titles` для dmp остался авто.
  Вопрос Семёну: переключить на B2B ИМЕННО путь _gen_campaign_content (промпт без авто-контекста + не-авто локальный фолбэк titles/texts/sitelinks + off `_upgrade_credit_titles` для B2B).
- Полный разбор: `scratchpad/batch_run_report.md`. Аккаунт porg-vfdnaolu ЧИСТ (0 камп). Харнесс: `scratchpad/batch_create.py` (2 фильтра), `batch_monitor/verify.py`, `pack_probe_all/alt.py`.
- **Осталось (шаг 5, Семён):** решение по kuderko (fill пака); долить точечно salamahin/terehov «Общее»; для tp6/tp7 — Метрика-аккаунт; dmp/gen_ses не трогались.
- **Прогнано 4 слепка, создано 19 черновиков (все DRAFT):** pavlov 9 (пилот), scherbakova 6, kryuchkova 4, gordeeva 0.
- **Харнесс (scratchpad, НЕ проект):** `batch_create.py` (set_plan → фильтр pay=='cpa' И tp6/tp7 → create_set_async
  via_cookie+no_cpa+launch=false), `batch_monitor.py`, `batch_verify.py`, `pilot_inv.py` (delete/inv), `pack_probe_all.py`.
  Запуск: `ssh lxc101-ts "/root/venv/bin/python3 /tmp/<script>.py ..."`. Отчёт: `scratchpad/batch_run_report.md`.
- **ДВА фильтра плана обязательны на porg-vfdnaolu:** (1) pay=='cpa' (пилот), (2) tp6/tp7 master/product —
  UNIFIED/SMART требуют счётчик Метрики, иначе `goals[0].goalId MUST_BE_VALID_ID value "0"` HTTP 400 (gordeeva: 51 мастер, все fail, ~10мин/item UAC-ретраи).
- **КОРЕНЬ «ушло в докрутку / 0 создано» = ПУСТОЙ М3-ПАК per-slepok, НЕ units** (координатор подозревал баллы — опровергнуто:
  докрутка шла «по куке» units-free и дала 0; cookie-путь баллы не трогает; прямой `_pack_for_item` показал from=fallback kw=0).
- **3 слепка с пустым паком на выбранном С пробегом:** gordeeva (но Мультибренд kw3538 ✅), karavaev (Мультибренд kw2683 ✅),
  **kuderko (пусто в ОБОИХ типах — нужен реальный fill)**. Остальные 7 — пак есть.
- **ОСТАНОВЛЕНО координатором** на диагностике (перед zubakin). Батч НЕ продолжен. Ждём решения Семёна: сменить
  site_type gordeeva/karavaev→Мультибренд + залить пак kuderko; для tp6/tp7 нужен Метрика-аккаунт. Аккаунт чист (0 камп).
- Известные системные (не баги прогона): tp5-«Модели» cookie→NO_BRAND_SEGMENTS (нужен токен), tp6/tp7→счётчик Метрики.

## Сессия 2026-07-11 — ПИЛОТ pavlov на porg-vfdnaolu (тест-прогон создания, 0 правок кода)
- **Инвентарь ДО:** 1 камп («Системная кампания eLama», DRAFT). Удалена штатно (Grid delete_campaigns) → 0.
- **Создано:** job `180d40beefe8`, via_cookie+no_cpa (cookie-Grid, без баллов). **9 черновиков** (все DRAFT):
  6 tp1 РСЯ (cpc) + 3 tp2 Поиск (cpc). tp3/4/5/6/7 пропущены (строгое соответствие профилю pavlov/С пробегом);
  фидов/yandex.xml нет → tp7 неприменим (single_feed на этом акке моот). Промо-автопромо (id 1975545) на все 9.
  Контент наполнен: tp2 Марки 26гр/1259кв, Модели 79гр/1681кв, Общее 3гр/254кв.
- **3 «failed»** = tp2 **cpa**-item'ы: harness послал их, а под no_cpa UI их НЕ шлёт (сервер no_cpa гасит
  только cpa-половину tp1/tp5-пар, standalone tp2 cpa НЕ фильтрует). + без Metrika-цели Яндекс отклонил:
  `PAY_FOR_CONVERSION_DOES_NOT_ALLOW_ALL_GOALS`. Не дефект продукта — артефакт harness.
- **Metrika-гейт:** porg-vfdnaolu без счётчика (`metrika_goals.counter_ids='[]'`). token-путь падает
  «укажите счётчик Метрики»; спасает `via_cookie AND no_cpa` (create_set_metrika.py:31 → optional).
- **Live-verifier: 1 error** NO_KEYWORDS_LIVE (tp2 Модели): 1 из 79 групп без ключей (пустой keyword-файл
  одной модели в паке). auto_repair НЕ чинит (skip), delayed content_repair (kind≠keywords) не покрывает →
  нужен keywords_repair (Grid AddKeywords, executable_now) ИЛИ create-side skip пустых search-групп.
- **Для БАТЧА:** (1) harness ДОЛЖЕН исключать pay=='cpa' item'ы при no_cpa (иначе спурьёзные fail);
  (2) аккаунтам без Metrika доступен только via_cookie+no_cpa (CPC-only); (3) NO_KEYWORDS empty-group —
  предложить Семёну create-side guard. porg-vfdnaolu ГОТОВ как пилот, дефектов-блокеров нет.

## Сессия 2026-07-11 — Терехов #49 (Блок 5): не-структурные правки уровня СОЗДАНИЯ РК — АНАЛИЗ, 0 правок
- **Итог:** реализовано 0 новых правок (консервативно, безопасность>полнота). Все 7 пунктов разобраны →
  `scratchpad/terehov_open_questions.md` (механика+что нужно от Семёна по каждому).
- **#6 tp2/tp4 split марки/модели — УЖЕ СДЕЛАНО** (фикс 2026-07-06, тот же porg-lzjk6p5m/terehov):
  segment=tp4_segment / only_cts=tp2_split_cts в create_set_text.py:66,70. Действий нет (verify на прогоне).
- **Не трогал (нужен ответ):** #1 r0088→r0134 = ДАННЫЕ Victory (r-код только из `_resolve_region` по городу,
  хардкода нет; комбо-код ag_part4). #2 гео-URL = `model_urls.py:_SITE_TYPE_URL_TPL/_model_page_href` глобальны +
  город под-специфицирован для мульти-город аккаунта cardealer-rus.ru (6 городов, одна кампания). #3 возраст =
  `create_set_master_product.py:625 age_lower` (сейчас age_35); terehov-scope возможен, но «с 24 до 55+» неоднозначно
  (age_25 vs age_18) → 1 строка готова после ответа. #4 Обмен авто / #5 город в заголовках = контент в ai_agents.py
  (ЗАПРЕЩЕНО, зона слепки-мастера). #7 tp7 feed-вариации = НОВАЯ архитектура (нет fid в кодере).
- НЕ деплоил, НЕ рестартил, кода не менял (py_compile не требуется). Готов реализовать #3/#2 (terehov-scoped) после ответов Семёна.


## 2026-07-11 — Тест tp7 (Товарка/SMART) с Метрикой из payload — goalId-фикс ПОДТВЕРЖДЁН
- Задача: проверить, что счётчик+цель из PAYLOAD (`prepare_metrika`) убирают goalId=0 у tp7.
  Дано: porg-vfdnaolu, counter=110499992/goal=579905467, agent=gordeeva override site_type=Мультибренд.
- Метод: standalone-драйвер на LXC101 (`DIRECT_ROLE=web`, test_client веб-приложения с форс-admin
  сессией, минуя auth) → delete_drafts → set_plan → фильтр(product&tcpa) → create_set_async → воркер.
- РЕЗУЛЬТАТ: ✅ 3 tp7-товарки (BAIC/Changan/Chery, ids 712717953/955/958) созданы created=3/failed=0/
  errors=0, БЕЗ goalId=0. Сырой UAC-детейл: **goal_id=579905467 (≠0), feed_id=3560490, ecom=true** ×3.
  Live-verifier: **status=pass, 0 issues**. Черновики ОСТАВЛЕНЫ (задача: «черновики оставь»).
- ⚠️ БЛОКЕР штатного пути: у porg-vfdnaolu 8 фидов, НИ ОДИН не в allow-list «Глобальных правил»
  (все `used-*`/`yandex-catalog*`, нет `/yandex.xml`) → set_plan(single_feed) даёт 0 product. Тест
  прошёл только с in-process разрешением ОДНОГО реального каталог-фида (3560490 yandex-catalog.xml).
  Полная секция = 29 tcpa товарки (58 items), прогнал субсет 3 для чистого goalId-сигнала.
- ⚠️ Латентный баг (не чинил, к Семёну): `create_set_plan.py:277` v5 feeds с невалидным FieldName
  `Url` → всегда «Некорректный запрос» → молчаливый фолбэк на Grid. Убрать `Url` из FieldNames.
- ERRORS_JOURNAL: TP7_GOALID_FROM_PAYLOAD ✅. Код НЕ менял (тест + верификация существующего фикса).

---
## 2026-07-11 — Batch-прогон авто-слепков porg-vfdnaolu (все КРОМЕ dmp/gen_ses) + сверка DoD
- ЦЕЛЬ: пересоздать все 11 авто-слепков (баллы, no_cpa=cpc-only, все 8 фидов), сверить live_verification.
- СДЕЛАНО: (1) 8 реальных фидов аккаунта занесены в `public.direct_global_feed_rules` enabled (sort 15-22,
  ОТКАТ: `DELETE ... WHERE sort BETWEEN 15 AND 22`). (2) Драйвер `direct/_tmp_batch_driver.py` (DIRECT_ROLE=web
  enqueue→worker; delete_drafts→set_plan(all variants)→create_set_async no_cpa/single_feed=false/via_cookie=
  false→poll Victory→live_verification). Валид: dry-карта всех 11 (профиль строго гейтит tp: у большинства
  tp1+tp2, у части +tp3/4/5/7). `direct/_tmp_dump_job.py` — дамп live_verification по job_id.
- ПРОГОН: pavlov LIVE (detach nohup `_tmp_batch_pavlov.log`). delete=3. Мультибренд job 207790a4997d:
  12 items, старт 10:58, к 11:35 done=1/12 created=2, БЕЗ 152. **tp1 NO_IMAGES фикс работает** (набор-
  preupload 725 картинок ДО цикла, per-РК = HIT).
- 🔴 БЛОКЕР: полный батч 11 слепков = МНОГОСУТОЧНО. OpenRouter моргнул 11:05 (mihomo-нода, порт 7890),
  через 7891 ожил; пока моргал — контент через M3 72B, ~1 item/10мин. gordeeva/МБ = ~232 UAC-товарки
  (8фидов×фан-аут, по куке). За 1 сессию все прогнать/сверить нельзя. Отчёт+решения+команда детач-раннера:
  `scratchpad/batch_run_dod_report.md`. Вопросы Семёну там (детач сутки+? товарку резать до каталог-фидов?
  orphan-фикс перед длинным прогоном?).
- ПРЕРВАНО НА: pavlov/Мультибренд идёт (detach). Остаток 10 слепков НЕ запускал (ждёт решения Семёна по
  многосуточному детач-прогону). Temp-харнесс (_tmp_batch_driver/_tmp_dump_job/_tmp_batch_results.json) на
  месте для продолжения.

### 2026-07-11 (продолжение) — Семён: режим БЫСТРОЙ DoD-валидации (не прод)
- КОНФИГ применён: llm_provider=openrouter (жив, 0.67s), товарка урезана до 4 каталог-фидов (used-*
  sort19-22 → enabled=false; ОТКАТ SET enabled=true WHERE sort BETWEEN 19 AND 22), no_cpa=cpc-only,
  via_cookie=false (баллы), orphan-фикс НЕ ставил, worker НЕ рестартил.
- ДРАЙВЕР `_tmp_batch_driver.py --fast`: 1 лёгкий site_type/слепок (Монобренд; kuderko→С пробегом).
  terehov=aon (мутация плана `_terehov_aon`: tp1/tp4/tp5 autotarget=True +переим КС→Автотаргетинг,
  tp2 только autotarget-строки). Мультибренд отвергнут для fast (tp1 МБ ~20мин/item, ~30 брендгрупп).
- РАННЯЯ ТОЧКА ✅: отменённая pavlov/Мультибренд (207790a4997d) успела 5 РК → live_verification **pass,
  errors=0, warnings=0, кодов НЕТ** (NO_IMAGES_LIVE отсутствует — фикс tp1-картинок работает; набор-
  preupload 693-725 картинок).
- ЗАПУЩЕН fast-батч детачем (`_tmp_batch_fast.log`), pavlov/Монобренд идёт. **ЗАТЫК = Яндекс API**
  (26 коннектов :443, объём групп/картинок/ключей на РК) ≈ минуты/РК → полный fast-батч (11 слепков)
  многочасовой. НЕ LLM (OpenRouter 0.67s). Результаты инкрементально в `_tmp_batch_results.json`,
  снапшот в таблицу: `_tmp_report_snapshot.py`. Отчёт: `scratchpad/batch_run_dod_report.md`.
- ПРЕРВАНО НА: fast-батч работает детачем (11 слепков Монобренд по очереди). Собирать результаты из
  results.json по мере готовности. Логин занимать нельзя (дедуп). worker НЕ рестартить.
- ⚠️ АНОМАЛИЯ 11:45: fast pavlov/Монобренд (794276a2bb6d) ВНЕШНЕ отменён (`control='cancel'`,
  лог «прервано пользователем перед листингами»), драйвер убит сигналом (БЕЗ трейсбека). Я НЕ отменял;
  авто-канселлера в коде НЕТ (`_cancel_children` — только дочерние с parent; watchdog ставит error/done,
  НЕ cancelled). Похоже на внешнее вмешательство через UI (`/api/create_set_cancel`). Батч ОСТАНОВЛЕН
  (драйвера нет, логин свободен, active-джоб нет). НЕ перезапускал вслепую — ждёт подтверждения Семёна
  (внешний оператор мог намеренно остановить). Харнесс на месте. Ранняя чистая точка (Мультибренд 5 РК,
  errors=0, NO_IMAGES-фикс OK) остаётся валидной.

---
## 2026-07-11 — Фича: вкладка «Структура слепков» → РЕДАКТИРУЕМАЯ (СБОРКА-only, НЕ деплоено)
- ЦЕЛЬ (объём Семёна): просмотр+редактор ключей группы; тумблер aon↔aoff сегмента; add/remove
  ct-группы (мастер из компонентов кодера, сырой gc запрещён). Всё — через ОБЩУЮ очередь как
  edit-джобы (не на лету), admin-only, dual-write пака, preflight, бэкап, аудит.
- НОВЫЕ ФАЙЛЫ: `slepki_editor.py` (ядро, Flask-free, configure DI: apply_edit_keywords/
  apply_toggle_aon_aoff/apply_add_ct_group/apply_remove_ct_group/handle_job/read_group_keywords +
  dual-write DST+M3 + backup + audit); `routes_slepki_edit.py` (6 эндпоинтов, admin-гейт).
- ИЗМЕНЕНО: `scripts/slepki_preflight.py` (+`preflight_dict(struct,profile)` чистая функция);
  `blueprint.py` (импорт+configure _sed; `_job_kind` возвращает edit-kind; воркер-диспетч ветка
  `_is_edit_job`→`_sed.handle_job`; done-блок и prefetch гейтят edit; регистрация роутов;
  `is_admin` в контекст шаблона); `templates/direct/index.html` (режим редактирования, edit-кнопки
  на группах/сегментах/tp, мастер add-ct, редактор ключей, JS).
- НОВЫЕ job-kinds (в теле `_kind`): edit_keywords, toggle_aon_aoff, add_ct_group, remove_ct_group.
- АРХИТЕКТУРА (обоснование): пул воркеров ПАРАЛЛЕЛИТ разные агентства → полный FIFO против create
  НЕ гарантируется. Реальный анти-гонки-механизм = АТОМАРНАЯ запись (temp+os.replace): create
  читает slepki_structure.json свежим на каждый `_json()` → видит старый ИЛИ новый ЦЕЛЫЙ снимок.
  targeting_profile.json КЭШИРУЕТСЯ в _btg → после записи сброс (_sed_profile_invalidate). Edit-джобы
  сериализуются между собой бакетом agency="" (_CREATE_MAX_PER_AGENCY=1).
- DUAL-WRITE: `_dual_write_pack_file` → DST (kp.PACK_ROOT, атомарно) + M3-источник (kp.M3_PACK_ROOT
  через ssh mkdir+tmp+mv). ok=DST.ok AND M3.ok → если ssh-запись упала, джоба = error (НЕ тихий
  orphan). Orphan-cleanup синка НЕ тронет файл: rsync тянет его из M3-источника в RAW → prune видит
  его в RAW → сохраняет. Только-DST (без M3) стёрся бы prune'ом — потому M3-запись обязательна.
- ПРОВЕРЕНО: py_compile OK (4 файла); pyflakes 0 undefined; node --check шаблонного JS OK;
  DRY на ВРЕМЕННЫХ копиях struct/profile+фейковые DST/M3: T1 read; T2 edit_keywords dual-write+
  дедуп(caseless)+trim; T2b минус-синтаксис reject; T3 toggle Марки→aon (profile КС370→Автотаргет370,
  gc-токены синхронны); T4 add ct0900 (gc собран из компонентов, размещён); T4b битый компонент→gc
  reject; T4c незарегистр. ct→reject; T5 remove; T6 preflight БЛОКИРУЕТ опустошение tp в профиле.
  Аудит jsonl пишется, бэкапы .editbak.<ts> создаются.
- НЕ деплоено/НЕ live (batch идёт, рестарт сиротит resumed): активацию — в окно тишины
  (рестарт direct-create + direct-create-worker). Боевые slepki_structure.json/targeting_profile.json/
  пак НЕ трогал — тесты на temp-копиях.
- LIVE-ПРОВЕРКА ПОСЛЕ ДЕПЛОЯ: (1) admin открывает вкладку → «✏️ Режим редактирования» → 🔑 на группе
  → правит ключи → в очереди job → воркер → файл в DST И на M3 (ssh m3-relay ls); (2) тумблер
  сегмента aon/aoff → targeting_profile.json + gc; (3) add/remove ct с preflight-отказом на
  опустошение tp. Non-admin: кнопок нет, эндпоинты 403.

---
## 2026-07-11 — ШАГ 0 редактор структуры ✅ LIVE + ШАГ 1 финальный DoD-прогон (детач)
- **ШАГ 0 (редактор структуры) ✅ ПОДТВЕРЖДЁН ЖИВЬЁМ.** Idempotent edit_keywords на непустой группе
  `karavaev/Монобренд/tp2/ct0000` (pavlov/Монобренд/ct0000 оказалась ПУСТА). Путь web-enqueue →
  worker → `slepki_editor.handle_job` → dual-write DST+M3 + аудит доказан: job cdc99d11041f done за 3с;
  DST `/opt/neuro_content_local/…/karavaev.txt` mtime 12:12:51 (было 07-01); M3 (ssh m3-relay) mtime
  00:12:53; аудит в public.direct_slepki_edits (id=1 ok=true) И slepki_edits_audit.jsonl (1 строка).
  Dual-write M3 НЕ упал (тихого orphan-ok нет). Байты 247→228 = trim/dedup, ключей 9pos/2minus неизменно.
  ⚠️ Оба сервиса ставят `NEURO_PACK_MOUNT=/opt/neuro_content_local` — ad-hoc python БЕЗ этого env читает
  sshfs `/opt/neuro_kontent` (другая копия) → при отладке редактора всегда задавать этот env. Харнесс
  `direct/_tmp_step0_editor_test.py`.
- **ШАГ 1 запущен ДЕТАЧЕМ** (pid 402595, 12:18): `_tmp_batch_driver --fast --jtimeout 14400`. Конфиг
  Семёна: no_cpa (=pays=[tcpa] cpc-only), **single_feed=True** (feeds=1 у всех 13, dry OK), via_cookie=
  False (баллы), openrouter, counter=110499992/goal=579905467. Все **13 слепков** (agent_list, вкл.
  gen_ses+dmp последними, terehov предпоследним aon). delete_drafts перед каждым. Fast=1 site_type
  (Монобренд; kuderko/gen_ses→С пробегом; dmp→dmp). ~151 items, ~15-20ч (затык=Яндекс API attach
  картинок). pavlov/Монобренд идёт (tp1 набор-preupload 550+ картинок = NO_IMAGES-фикс работает).
  worker НЕ рестартить (сиротит resumed). Результаты `_tmp_batch_dod_results.json`, лог
  `_tmp_batch_dod.log`, снапшот `python3 -m direct._tmp_report_snapshot`. Отчёт
  `scratchpad/batch_run_dod_report.md`.
- ПРЕРВАНО НА: детач-прогон работает (pavlov первым). Собрать таблицу к завершению. DoD-фокус: dmp=B2B
  (не авто) на последнем слепке; NO_IMAGES_LIVE=0 на tp1. Убрал старый харнесс (_tmp_batch_pavlov/fast/
  full.log, _tmp_batch_results.json). Прибрать temp-файлы после сбора отчёта.

---
## 2026-07-12 — DMP B2B: слепок собран по выгрузкам + 4 фикса (задеплоено на LXC101)
Прогоны `porg-lrfjzcxo` (агентство y-direct-victory): tp2 создаётся 8/0, блокер снят. Все правки — в
локальных Mutagen-файлах + БД Victory, задеплоено, `direct-create`+`worker` рестартнуты. Детали — в
ERRORS_JOURNAL (5 записей 2026-07-12) и DOD 5.a/5.b.
- **Блокер снят:** `kontent_pack.py:gather` читал ssh на ПРОТУХШИЙ M3 (ct0001–34) вместо локального
  зеркала 101 (ct0800+) → «пак M3 пуст», tp2 в defer. Фикс — gather local-first. Теперь tp2 создаётся.
- **#2 Пак:** наполнен из выгрузок кабинета (dmp3.zip, 5 xlsx) — 34 ct (ct0800–ct0833), 1204 ключа,
  вкл. ранее отсутствовавшие ct0822–ct0833. Бэкап `dmp/tp2.bak_vygruzka_*` на 101.
- **#3 Имена групп:** были «— Авто» (авто-фид). Фикс: `leadgen_ct_naming` (БД) выровнен по выгрузке
  (34 ct = темы: Идентификация/Определение/…); код — имя не-авто = leadgen→структура→ct, без авто-фида
  (`create_set_tp1_builders.py::_struct_ct_names`).
- **#1 Сайтлинки:** были авто (автокредит/КАСКО). Фикс: `ai_content.py` нормализует строки→dict +
  `direct_slepok_content` dmp = 8 B2B-ссылок.
- **#4 Тип оплаты:** единый `pays = ["tcpa"] if no_cpa else ["tcpa","cpa"]` для ВСЕХ слепков (tp2/tp4/МК);
  галочка «под стиль сайта» активна → cpc+cpa, снята → только cpc. dmp: активна 16 РК / снята 8.
- **ct0834** = «МК Конкуренты» (выделенный, вне авто; ct0084=FAW НЕ использовать).
- ⬜ ОСТАЁТСЯ: картинки МК dmp тянут авто-салоны (UAC_MEDIA_MISSING, DOD 5.a6); ct0834 не внесён в
  leadgen_ct_naming (не критично); докрутка иногда падает на куке (`_existing_names пуст`) — сессия аккаунта.
- ПРОВЕРИТЬ ЖИВЬЁМ: пересоздать dmp с галочкой (16) и без (8); имена групп как в выгрузке (не «Авто»);
  сайтлинки B2B; ключи реальные из выгрузок.

---
## 2026-07-13 — Тон-войс аудит созданных РК + watcher (LXC101, active)
- **НОВЫЙ инструмент** `direct/tools/check_tone_of_voice.py`: после set-джобы читает реально
  сгенерированные заголовки/тексты созданных кампаний и через LLM-судью (OpenRouter, дешёвая
  модель = `_or_complete_url` из llm_providers) оценивает, в ГОЛОСЕ ли слепка контент или это
  generic-кредитный шаблон → краткий вердикт в ЛИЧНЫЙ telegram (`loader.send_telegram_message`).
  Порог `THRESHOLD=60`. Режимы: `<job_id>` / `--latest` / `--login` / ad-hoc `--agent+--login`.
  READ-ONLY (v5 ads.get + cookie-Grid fallback + Victory DB).
- **Ключевое:** контент в result джобы НЕ хранится (`slepok_content=None`) → читаем LIVE. v5
  `token_for_login` может ЗАЛИПНУТЬ на токене с пустым доступом (empty≠error) → перебираю все
  агентские токены, беру тот, что реально отдаёт объявления. Голос слепка = `ai_agents.AGENTS[s]`
  (system/tagline/promo) + `CROSS_SIGNATURE.get(s)` (есть только у 5 базовых!) + `AGENT_ADS[s]` корпус.
- **Watcher** `direct/tools/tone_of_voice_watcher.py` + `deploy/tone-of-voice-watcher.service`
  (enabled+active на LXC101, интервал 90с). ОТВЯЗАН от горячего пути создания. Дедуп через
  `public.direct_tone_checks` (job_id PK); курсор в файле `.tone_watcher_cursor` = NOW() на первом
  старте (историю НЕ проверяет, без спама). Только kind='set' status='done'.
- ВЕРИФИЦИРОВАНО ЖИВЬЁМ: v5 read 5 кампаний porg-lrfjzcxo (dmp), LLM score 100 in_voice, telegram
  sent=true ([TEST]); graceful на удалённых черновиках job 0d2708907d6d («не смог проверить»).
  Горячий путь не тронут (только новые файлы). Остановить: `systemctl disable --now tone-of-voice-watcher`.

---
## 2026-07-13 — Content Editor: перестановка порядка быстрых ссылок (sitelink_reorder) — доведено до ума + верифицировано вживую
- Фича `sitelink_reorder` (routes_content_editor.py `_reorder_sitelinks`, UI `content_editor.html`
  `ceReorder*`) висела в рабочем дереве незакоммиченной — доведена сегодня по итеративной
  проверке Семёна в браузере, живьём на `porg-psm5h7q6`.
- **UI:** чипы позиций — столбец, номер = ТЕКУЩАЯ позиция (обновляется при drag), title+description
  на чипе, инлайн ✏️-редактор (тот же флоу sitelink_title/description, что список ниже). Убран
  ручной селектор «Позиций в перестановке» — длина берётся автоматически (мода). Добавлен
  выпадающий список «Все наборы / конкретный набор» (`RO_TARGET_IDX`) — перестановка может
  таргетить один конкретный набор, не только все 15+ разом (`target_set_id` в задании).
  UAC (tp6/tp7) для пользователя визуально НЕ выделяется отдельной категорией (тег/подпись как
  у обычного набора) — реализация внутри разная, наружу единообразно.
- **ResponsiveAd:** предупреждение о непереставляемых объявлениях теперь ТОЧНОЕ — `_load_account`
  считает реальный `responsive_count`/`responsive_examples` (кампания/группа) на набор вместо
  безусловного «на всякий случай» для любого ad-level набора.
- **Очередь (админ-панель):** кнопка «🧹 Очистить очередь» — `POST
  /admin/queue/cleanup` удаляет завершённые (done/error/cancelled) задания старше 3 суток из ОБЕИХ
  таблиц (content_jobs + price_check_jobs), активные не трогает (`price_check.cleanup_old_jobs`).
- **Верификация вживую (НЕ на словах):** сверил реальный HAR браузерного UAC PATCH — payload-билдер
  `_uac_campaign_patch_payload`/`_UAC_PATCH_FULL_KEYS` совпал 1-в-1. Прогнал НАСТОЯЩИЙ
  `sitelink_reorder`-джоб через очередь на живой UAC-кампании 712694743 (`porg-psm5h7q6`):
  applied_sets=1, независимый read-back (GET, не веря отчёту джобы) подтвердил смену порядка на
  сервере Яндекса → откатил тем же self-inverse свопом обратно, аккаунт остался чистым.
  162 набора аккаунта прочитаны и промоделированы (`_apply`) корректно, без ошибок.
- ⚠️ **Важно на будущее:** у content editor ДВА процесса — `direct-content.service` (веб) И
  `direct-content-worker.service` (очередь, держит модуль в памяти). Правка кода записи требует
  рестарта ОБОИХ — рестарт только веба воркер не обновляет (грабли поймали в этой же сессии).
- Файлы: `routes_content_editor.py`, `templates/direct/content_editor.html`, `price_check.py`
  (`cleanup_old_jobs`), `content_worker.py` (не трогали в этой сессии — уже был обновлён ранее).
- ОСТАЁТСЯ: ничего не сломано и не отложено — фича закрыта. Если нужна content-aware перестановка
  (по названию ссылки, а не по позиции) — отдельная задача, не делали.

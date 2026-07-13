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
- Статус: 🟡 фикс на Mac (Mutagen→LXC101 авто), py_compile OK. Ждёт живого прогона добивки на кампании с gap-ct: images_repair должен вернуть ok=True + content_gap_cts, без «ни для одного ct».
- НЕ помогло ранее: — (первая правка разделения контент-гэп/upload-fail в images_repair). Reschedule авто-добивки уже ограничен `_DELAYED_REPAIR_MAX_RESCHEDULES=1` / `_DELAYED_FULL_REPAIR_MAX_ITERATIONS=2` (queue_server.py:66/69) — вечного цикла на gap-ct нет; правка убирает ложный hard-fail и вводящее в заблуждение сообщение.

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
- НЕ помогло бы: держать Конкуренты на ct0084 — притянул бы авто-бренд FAW (контент/имя) = то самое «пересечение с авто».
- Открытый долг (не блокирует фикс): `leadgen_ct_naming` без имён ct0822–ct0834 (naming gap); имена структуры vs coder расходятся (ct0801 структура=«СОЦ сети» vs coder=«Идентификация»); зарегистрировать ct0834; жёсткий slepok-фильтр в `_score` для не-авто (анти-bleed).

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
- Статус: 🟡 фикс задеплоен + gather подтверждён; ждёт боевого прогона (tp2 dmp должны создаваться, не defer).
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

### SLEPOK_SYNTHETIC_STRUCTURE_COLLISION — «все слепки дают одинаковую структуру»
- Симптом: какой слепок ни выбери — итоговая структура/группы практически одинаковые.
- Где: `slepki_structure.json` (данные, НЕ код).
- Root-cause: файл стал СУПЕРСЕТОМ с байт-идентичными секциями `items` у РАЗНЫХ слепков (новые слепки
  заводили синтетически, копируя общий набор). `_slepok_struct_groups` честно читает `directologists[<slepok>]`,
  но там одна и та же ветка данных → одинаковая структура. Фолбэка на чужой слепок в коде НЕТ — данные реально дублировались.
- **DoD-ПРАВИЛО (2026-07-10):** слепок собирается ТОЛЬКО из реального корпуса директолога (харвест его живых
  аккаунтов вкл. выключенные/удалённые, Grid-перебор кук; для tp6/tp7 UAC — только Grid, v5 не видит).
  Синтетические заглушки ЗАПРЕЩЕНЫ. Новый слепок — только из реальных выгрузок/аккаунтов, не копипаст суперсета.
- **Preflight перед деплоем:** `python3 scripts/slepki_preflight.py` — падает (exit 1), если есть
  cross-slepok коллизии / пустые группы / пустые tp, открытые в targeting_profile.json. Гонять ПЕРЕД мерджем в боевой.
- Решение (2026-07-10): пересборка 13 слепков из реального корпуса (генератор `scripts/build_slepok_structure.py`),
  cross-коллизии 6→0, 81 пустая группа удалена. Голос AGENT_ADS 13/13. Коммиты dcf212b + 63229c7.
- Статус: 🟡 задеплоено, ждёт живого прогона (контент-тест #51 подтвердил различие тона 5/9 чётко).
- НЕ помогло ранее: —

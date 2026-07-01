# EXTRACTION_PLAN.md — вынос `/direct/automation` в отдельный сервис

> Статус: **Фазы 1-3 выполнены.** Решение зафиксировано 2026-06-22,
> реализация и cutover выполнены 2026-06-30.

---

## Решение (коротко)

| Что | Вердикт | Когда |
|-----|---------|-------|
| Поиск по коду + точечный `rg/Read` | ✅ делать сейчас | сразу |
| Вынос Директа в отдельный процесс `direct.service` | ✅ делаем — закрывает «упал один, второй жив» + боль рестарта | начато |
| Разбивка `blueprint.py` (~14.9k строк) на модули | ⏸ после отдельного процесса | только маленькими scoped-патчами |
| Полный микросервис (API+фронт раздельно) | ❌ избыточно для одного LXC | — |

**Почему не «оставить как есть»:** весь `seoadvanced.ru` (телеграм-дайджест, work, спорт,
todo, delta + Директ) живёт в ОДНОМ процессе `digest.service`. Зависший SSH к M3 или
воркер создания РК кладёт весь сайт. **Почему не срочно:** работает и закалено (таймауты,
LRU-кэш, восстановление заданий из БД).

---

## Почему выносится легко

`direct/` уже почти автономен:
- Свои модули: `blueprint.py` (~14.9k), `campaign.py`, `ai_agents.py`,
  `kontent_pack.py`, `grid_create.py`, `grid_finalize.py`, `promo.py`, `promotions.py`.
- **Ноль импортов** из соседних блюпринтов (`work/delta/sport/todo/telegram_parsing`).
- Наружу — только внешние системы: Victory PG (`local_gsheet_sites`, `metrika_goals`,
  `direct_*`), Direct API v5/v501 (OAuth), Grid (куки главпотока), M3 (LLM + контент-пак).
- Своей таблицы в БД `seoadvanced` нет.

## Связи с хостом (что отвязать)

| Связь | Сейчас | При выносе |
|---|---|---|
| Процесс/порт | в `app.py`, 5010 | свой `direct.service`, **127.0.0.1:5020** |
| Роутинг | blueprint `/direct/*` | nginx: `location /direct/ → 127.0.0.1:5020` (proxy_pass БЕЗ слэша) |
| Авторизация | `auth.py` + Flask-session cookie | **тот же `FLASK_SECRET_KEY`** → SSO бесплатно, логин на главном |
| Шаблоны/нав | `templates/direct/` + общий layout + `inject_nav_context` | перенести context-processor в `direct/main.py` или свой shell |
| Реестр прав | `work:direct` в `_BUILTIN_SECTIONS` (`app.py`) | **остаётся там же**, не трогаем |
| Секреты/БД/куки | `.secret/loader.py` выше по дереву | путь сохраняется, Mutagen синкает |

---

## Фазы

### Фаза 0 — заморозка карты связей
Подтвердить `get_impact_radius` по `direct.blueprint` — внешних зависимых нет (граф уже даёт 0 импортов).

### Фаза 1 — entrypoint `direct/main.py` — ✅ готово
```
app = Flask(__name__, template_folder=..., static_folder=...)
app.secret_key = _get("FLASK_SECRET_KEY")        # ТОТ ЖЕ ключ = SSO
app.permanent_session_lifetime = timedelta(days=30)
app.register_blueprint(direct_bp)                # url_prefix="/direct"
init_direct()
app.run(host="127.0.0.1", port=5020, use_reloader=False)   # use_reloader=False ОБЯЗАТЕЛЬНО
```
Файл добавлен: `direct/main.py`. Шаблоны и статика берутся из общего `home/seoadvanced`.
Из `app.py` убраны `register_blueprint(direct_bp)`, импорт `direct_bp` и `_init_direct()`.

### Фаза 2 — авторизация без второго логина — ✅ локально готово
Flask-session = подписанный cookie. Один `FLASK_SECRET_KEY` + один домен `seoadvanced.ru` ⇒
cookie главного сайта валиден в Директе; `user_services` лежит в cookie ⇒ декоратор
`_service_required_any("work","work:direct")` работает как есть.
- `auth.py` не дублируем. В `direct/main.py` добавлены совместимые endpoints `login`/`home`,
  которые редиректят на `/login` и `/`. Это закрывает `url_for("login")`/`url_for("home")`
  без расхождения двух копий auth-кода.

### Фаза 3 — nginx + systemd (LXC 101) — ✅ готово
- nginx: `location /direct/ { proxy_pass http://127.0.0.1:5020; proxy_set_header Cookie $http_cookie; }`
- `/etc/systemd/system/direct.service`: `ExecStart=<venv>/python .../direct/main.py`, `Restart=always`
  (env/user/venv — скопировать из `digest.service`).
- Деплой: Mutagen синкает папку → `systemctl restart direct.service`. Главный сайт НЕ падает.
- Шаблоны добавлены:
  - `direct/deploy/direct.service`
  - `direct/deploy/nginx-direct-location.conf`
- На LXC101 установлен и включён `direct.service` (`127.0.0.1:5020`).
- nginx переключён: `/direct/` идёт в `direct.service`, всё остальное — в `digest.service`.
- Backup nginx: `/etc/nginx/sites-available/seoadvanced.ru.bak.20260630_211541`.

### Фаза 3.5 — параллельный прогон / smoke
Выполнено без создания кампаний:
- `5010/direct/automation → 404` после удаления Direct из main app;
- `5020/direct/automation → 302 /login`;
- публичный `https://seoadvanced.ru/direct/automation → 302 /login`, запрос виден в `direct.service`.

Осталось для полной бизнес-приёмки: авторизованный UI-smoke и безопасный dry-run/черновик на
`porg-psm5h7q6`, когда это не конфликтует с текущей работой в аккаунте.

### Фаза 4 (опционально) — web + worker
Если зависания M3/Яндекса будут ронять даже веб-морду Директа — вынести фоновый воркер
(`_create_worker_loop`) в `direct-worker.service`, общение через jobs-таблицу (уже есть:
`_jobs_db_*`, `_claim_next_job`). Пока — держим в уме.

### Фаза 4.5 — автопредсоздание промоакций по аккаунтам
Существующий код уже умеет засевать БД-библиотеку слепков:
- `_seed_slepok_content()` создаёт `direct_slepok_content`;
- `kind='promo'` генерируется через M3 по слепку, при сбое берётся deterministic fallback;
- ручной endpoint: `/direct/api/ai/slepok_content/seed`;
- просмотр: `/direct/api/ai/slepok_content`;
- публикация в аккаунт: `/direct/api/ai/promo/publish`.

Что заложить после стабилизации `direct.service`:
- автозадачу в Direct-service, которая при открытии/обновлении аккаунта проверяет наличие
  `direct_slepok_content(kind='promo')` для `directologist × site_type`;
- если промо отсутствуют или устарели — ставит lightweight job на M3-предсоздание без публикации;
- в аккаунт промо публикуется только явным действием пользователя или отдельным подтверждённым batch,
  потому что это меняет библиотеку Яндекс.Директа клиента.

### Фаза 4.6 — post-create verification / добивка
Статус: **v1 готов, v2 расширен до content-repair для tp2/tp5.**

- `verifier.py` — статический отчёт после `create_set`: id, кодер-имена, ошибки, промо/callouts,
  repair_candidates.
- `live_verifier.py` — read-only нормализация созданных кампаний и сверка факта по уже полученным
  снимкам Grid/v5.
- `grid_read.py` — read-only Grid-счётчики фактических `adGroups/ads` по campaign ids. Live-сверка
  теперь добавляет `NO_ADGROUPS_LIVE` / `NO_ADS_LIVE` для tp1–tp5, чтобы `rebuild_missing_content`
  планировался по факту кабинета, а не только по сохранённому JSON результата.
- `uac_read.py` — read-only UAC детали tp6/tp7 через `/web-api/uac/campaign/{id}`: live-сверка
  проверяет фактические `titles/texts/sitelinks/media/feed` и планирует `resume_or_recreate_campaign`
  при `UAC_*_MISSING`, без v5 units.
- `repair_planner.py` — чистый план добивки: превращает issues/repair_candidates в действия
  (`resume_or_recreate_campaign`, `rebuild_missing_content`, `create_or_attach_promo`, `ensure_callouts`,
  `rename_campaign`) и помечает транспорт. Дефолт — cookie/Grid, `uses_direct_units=false`.
- Endpoint: `/direct/api/create_set_verification?job_id=...`; `live=1` включает live-сверку.
- `create_set` теперь сам возвращает `live_verification` сразу после статической проверки:
  автоматический read-only Grid-first обход без v5-баллов.
- Endpoint: `POST /direct/api/create_set_repair` — repair-gate. Он поднимает сохранённый
  result job, заново делает Grid-first live-сверку и возвращает `repair_plan`. `execute=1`
  умеет scoped retry/recreate: выбирает исходные items для `resume_or_recreate_campaign` и
  ставит отдельную repair-job в очередь `create_set` (`via_cookie=true`, `launch=false`, без v5 units).
  `rebuild_missing_content` для tp2/tp4 добивает группы и adaptive text ads прямо в существующий
  `campaign_id` через Grid/cookie; для tp3/tp5 добивает товарную группу + ShoppingAd/ListingAd с
  vendor/model/name фильтрами, без повторного создания кампании и без Direct API units.
  Для `UAC_*_MISSING` включён replace-flow: удалить конкретный неполный tp6/tp7 draft по UAC id,
  затем поставить исходный item в repair queue с `_repair_force_names`, чтобы `RESUME-SKIP` не
  пропустил пересоздание из-за старого имени в Grid-кэше.
  In-place executors: `create_or_attach_promo` создаёт согласованное промо через Grid по выбранному
  слепку и привязывает его к campaign ids из завершённой job; `ensure_callouts` создаёт/находит
  выбранные уточнения через Grid и обновляет только `inheritableCallouts`; `rename_campaign`
  исправляет потерянное/старое имя через узкий Grid `UpdateCampaigns` только с полем `name`.
  UAC tp6/tp7 content-in-place пока остаётся planned-only.
- Важно по баллам: дефолт live-сверки = **Grid/cookie first**, `v5=1` только опционально, потому что
  баллов Direct мало, а tp6/tp7 в v5 не видны.

Следующий слой: UI-кнопка/индикация execute-добивки и более детальный UAC post-repair отчёт, если
скрытый UAC detail стабильно отдаёт поля в разных аккаунтах. v5 использовать только для сущностей,
которые невозможно исправить Grid/UAC-путём.

### Фаза 5 — разбивка `blueprint.py` (ОТЛОЖЕНА)
Один `bp`, много файлов: `web.py`, `yandex_api.py`, `accounts.py`, `campaigns_ops.py`,
`create_set.py`, `minus.py`, `ai_routes.py`. Делать строго по одному модулю за коммит.
+ создать `direct/_MAP.md` (endpoint→функция→строка). Делать только при росте боли поддержки.

---

## Риски (из кода)

### 🔴 Высокие
1. **`url_for("login")` → 500** в отдельном процессе (нет эндпоинта) — заменить на абсолютные пути.
2. **Строго ОДНОПРОЦЕССНО:** очередь/троттлинг/ротации — in-memory глобалы (`_CREATE_JOBS`,
   `_CREATE_QUEUE`, `_PULL_LAST`, `_TITLE_PROMO_IDX`…). `gunicorn -w >1` или `use_reloader=True`
   → задвоение воркера, двойное создание РК. Только 1 процесс.
3. **Двойное владение jobs-таблицей при cutover:** если оба сервиса живы и смотрят в одну
   таблицу — `_jobs_db_recover()` поднимет и выполнит задания дважды. Cutover атомарный:
   сперва убрать из `app.py` + рестарт digest, потом поднять `direct.service`.

### 🟡 Средние
4. nginx `proxy_pass` без завершающего слэша (со слэшем срежет `/direct/` → 404).
5. Навигация: `inject_nav_context` (context-processor) — перенести или дать свой shell.
6. SSO рушится при расхождении `FLASK_SECRET_KEY` или домена.
7. Статика `/static/...` — решить, кто отдаёт.

### 🟢 Низкие
- +~150 МБ RAM; бинд только `127.0.0.1`; больше коннектов к PG (в норме);
  настоящего SSE в Директе нет (nginx-буферизацию не трогать ✅);
  M3 SSH-mux после выноса трогает только Директ → конфликтов меньше ✅.

### ⚠️ Риски разбивки файлов (фаза 5)
8. Циркулярные импорты — строгая слоистость web→api→domain.
9. Расползание module-level глобалов — каждый глобал в одном модуле (или `state.py`).
10. «Два окна» — высокая текучка → по одному модулю за коммит, маркеры A/B.

---

## Откат
Полностью обратимо: вернуть `register_blueprint(direct_bp)` + `_init_direct()` в `app.py`,
остановить `direct.service`, вернуть nginx.

# План реструктуризации `home/seoadvanced/direct`

> Статус: **ПРЕДЛОЖЕНИЕ, не исполнено.** Review-first — применять только по команде Семёна.
> Дата: 2026-07-29. Сервис живой: https://seoadvanced.ru/direct/automation

---

## 1. Факты (замерено, не на глаз)

| Метрика | Значение |
|---|---|
| Файлов в папке всего | 14 677 |
| Файлов в **корне** папки | 274 |
| Из них `.py` | 197 (**184 реальных** + 13 мусорных `._*`) |
| Из них `.md` | 42 |
| Прочий мусор в корне | ~19 (логи, `_probe_*`, `.DS_Store`, `.bak_*`, `direct.db`) |
| systemd-юнитов на проде | 12 |
| Точек входа (`*_main.py`, `main.py`) | 10 + `content_worker`, `slepki_bot` |
| Потребителей пакета `direct` **вне** папки | **0** |

### Кластеры уже существуют — просто закодированы в имена файлов

| Префикс | Файлов | Смысл |
|---|---|---|
| `create_set_*` | 36 (+`create_content`, `create_job_status`) | создание РК |
| `copy_*` | 35 | копирование кабинетов |
| `routes_*` | 19 | web-слой Flask |
| `content_*` | 13 | контент-редактор |
| `repair_*` | 8 | ремонт кампаний |
| `slepki_*` / `slepok_*` | 7 | слепки |
| `grid_*` | 5 | Grid API |
| `campaign_*` | 5 | доменная модель кампании |
| `blueprint_*` | 4 | чертёж кампании |
| `ai_*` | 4 | LLM-контент |
| `uac_*`, `price_*`, `detect_*` | по 3 | прочее |

**Вывод:** мы не проектируем структуру с нуля, а материализуем ту, что уже есть.
Это резко снижает риск спора «а где чему место».

---

## 2. Что делает переезд возможным

### 2.1 Пакет замкнут

`grep` по всему `home/seoadvanced` даёт **ноль** файлов вне `direct/`, которые импортируют `direct`.
Взрывной радиус ограничен самой папкой + 12 юнитов + Mutagen.

### 2.2 Главная форма импорта — `from direct import X` (112 случаев)

```python
from direct import blueprint as bp
from direct import campaign as cmc
from direct import yandex_gateway as _yg
```

`direct/__init__.py` сейчас **пустой (0 строк)** — это работает только потому, что Python
при `from package import module` сам подгружает подмодуль.

👉 **Это и есть механизм совместимости.** После переезда `campaign.py` → `core/campaign.py`
одна строка в `__init__.py` чинит все 112 мест разом:

```python
from direct.core import campaign          # noqa: F401  (shim: старый путь `from direct import campaign`)
```

Ни один вызывающий файл править не обязательно. Импорты можно чистить потом, спокойно,
не под нагрузкой.

### 2.3 Прямые `from direct.<module>` — 49 уникальных модулей

Эти нужно править механически: `from direct.copy_engine` → `from direct.copy.engine`.
Правится `sed`-ом по точному списку, проверяется `py_compile` всего пакета.

---

## 3. Мины — где «просто перенести» ломается молча

| # | Где | Что | Почему опасно |
|---|---|---|---|
| M1 | `routes_autorules.py:635` | `importlib.import_module(f".{module_path}", package="direct")` | путь модуля приходит **строкой из данных** (реестр сенсоров). Ни линтер, ни `py_compile` не увидят |
| M2 | `copy_engine.py:69` | `importlib.util.spec_from_file_location(...)` по пути на диске | путь файла зашит, переезд рвёт |
| M3 | `text_gen.py:24`, `ai_agents.py:30`, `content_worker.py:76` | `importlib.reload(text_norm)` | зависит от того, как модуль был импортирован изначально |
| M4 | `slepki_editor.py:49`, `automation_runtime.py:28` | `importlib.util` | проверить руками |
| M5 | 12 юнитов `deploy/*.service` | `ExecStart=... -m direct.<entry>` | переименование точки входа = правка юнита + рестарт |
| M6 | **Mutagen** | папка синкается на LXC 101 автоматически | файл уехал локально → он **мгновенно на проде**, до правки юнитов. Полупереехавшее состояние на живом сервисе — самый вероятный способ уронить `/direct/automation` |
| M7 | второе окно Claude | 14 незакоммиченных файлов прямо сейчас | двигать файлы под чужой активной работой нельзя |

**M6 — главная.** Ни один шаг с кодом не начинать, не поставив Mutagen на паузу.

---

## 4. Целевая структура

```
direct/
├── __init__.py          ← реэкспорт-shim'ы (временно), потом чистится
├── main.py              ← ТОЧКИ ВХОДА ОСТАЮТСЯ В КОРНЕ
├── accounts_main.py        (12 systemd-юнитов не трогаем вовсе)
├── ai_main.py
├── autorules_main.py
├── content_main.py
├── content_worker.py
├── copy_main.py
├── gateway_main.py
├── slepki_main.py
├── slepki_worker_main.py
├── slepki_bot.py
├── worker_main.py
│
├── CLAUDE.md  README.md  STATE.md  INDEX.md  MEMORY.md   ← только эти 5 доков в корне
│
├── core/            # инфраструктура, от которой зависят все
│   ├── automation_runtime.py   queue_server.py
│   ├── direct_repository.py    job_repository.py
│   ├── write_gate.py           stage_timing.py
│   └── README.md
├── clients/         # внешние API
│   ├── direct_v501_client.py   yandex_gateway.py   gateway_client.py
│   ├── grid_read.py  grid_create.py  grid_create_payloads.py
│   ├── grid_finalize.py  grid_content_verifier.py
│   ├── uac_client.py  uac_read.py  uac_verifier.py
│   └── README.md
├── campaign/        # доменная модель
│   ├── campaign.py  campaign_naming.py  campaign_result.py
│   ├── campaign_spec_audit.py  campaign_state_verifier.py
│   ├── blueprint.py  blueprint_metrika.py  blueprint_targeting.py
│   ├── blueprint_content_rules.py
│   └── README.md
├── create/          # 38 файлов
│   ├── create_set_*.py  create_content.py  create_job_status.py
│   ├── precreate.py  seed_slepok_content.py
│   └── README.md
├── copy/            # 35 файлов (copy_main.py остаётся в корне)
│   ├── copy_*.py
│   └── README.md
├── content/         # 13 файлов (content_main/worker в корне)
│   ├── content_*.py  text_gen.py  text_norm.py
│   ├── ai_content.py  ai_agents.py  ai_agents_data.py  llm_providers.py
│   ├── kontent_pack.py  promo.py  promo_gen.py  promotions.py
│   └── README.md
├── slepki_code/     # ⚠ имя иное: slepki/ уже занята ДАННЫМИ
│   ├── slepki_editor.py  slepki_publish.py  slepki_store.py
│   ├── slepok_qa_run.py  pack_resolver.py
│   └── README.md
├── repair/
│   ├── repair_*.py  verifier.py  verification_service.py
│   ├── live_verifier.py  local_result_verifier.py
│   ├── detect_*.py
│   └── README.md
├── web/             # 19 routes_*
│   ├── routes_*.py
│   └── README.md
├── pricing/
│   ├── price_check.py  price_check_cron.py  price_check_apply_watch.py
│   ├── feed_models.py  model_urls.py  link_check.py
│   └── README.md
├── util/
│   ├── city_morph.py  geo_strip.py  account_service.py  account_filters.py
│   └── README.md
│
├── autorules/       ← уже есть
├── deploy/  docs/  scripts/  slepki/  tests/  tools/  dev/   ← уже есть
└── var/             ← НОВОЕ, в .gitignore: логи, _probe_*, кэши, direct.db
```

Корень: **12 точек входа + 5 доков + `__init__.py`** ≈ 18 файлов вместо 274.

---

## 5. Требование: у каждого сервиса — свои `.md`

Сейчас 42 дока лежат общей кучей, и понять, какой к какому сервису — нельзя.
Правило после реструктуризации:

### 5.1 Обязательный минимум на каждый пакет

Каждая папка из §4 обязана иметь **`README.md`** со схемой:

```markdown
# <пакет> — <одна строка что это>

## Зачем
2-4 строки: какую задачу решает, кто дёргает.

## Точка входа
Какой systemd-юнит / какой *_main.py его поднимает. Если библиотека — так и написать.

## Файлы
| Файл | Отвечает за |
|---|---|
| ... | ... |

## Инварианты
Что нельзя ломать. Со ссылкой на код: `файл.py:строка`.

## Как проверить
Конкретная команда + что считается «работает».
```

`CLAUDE.md` в пакете заводится **только если** у пакета есть свои жёсткие правила для ИИ,
которых нет в корневом. Не плодить ради симметрии.

### 5.2 Куда разъезжаются существующие 42 дока

| Док | Куда |
|---|---|
| `CLAUDE.md` `README.md` `STATE.md` `INDEX.md` `MEMORY.md` | **корень** (остаются) |
| `ARCHITECTURE.md` `UI_MAP.md` `ERRORS_JOURNAL.md` | `docs/` |
| `CAMPAIGN_INVARIANTS.md` `CREATION_PROTECTED_RULES.md` `BLUEPRINT_SPLIT_PLAN.md` | `campaign/` |
| `COPY_INDEX.md` `COPY_README.md` `STATE_COPY_OTHER.md` | `copy/` |
| `CONTENT_EDITOR.md` `CONTENT_EDITOR_COOKIE_GRID.md` | `content/` |
| `CODER.md` `SLEPKI_AUDIT_2026-07-12.md` `SLEPKI_BOT_PLAN.md` `SLEPKI_REBUILD_PLAN.md` `STRUCTURE_EXCLUSIONS.md` `slepok_qa_report.md` | `slepki_code/` |
| `COOKIES_STATUS_CHECKER.md` | `clients/` |
| `POSEVY_AUTORULES_PLAN.md` | `autorules/` |
| `DOD.md` | корень или `docs/` — решает Семён |
| `*_ARCHIVE.md` (5), `ARCHITECTURE_AUDIT_2026-07-12.md`, `EXTRACTION_PLAN.md` | `docs/archive/` |
| `_PROPOSAL_*` (5) + `_proposed_piterkina_delete.md` | `docs/proposals/`, отработавшие — удалить |

### 5.3 Как это не сгниёт снова

- `scripts/gen_project_index.py` уже умеет собирать `INDEX.md` по папкам — после переезда
  он даст оглавление автоматически, и хук `maintenance.py` будет обновлять его раз в сутки.
- Правило в корневой `CLAUDE.md`: **новый `.py` кладётся в пакет, не в корень.**
  Корень зарезервирован под точки входа.
- Правило: правишь пакет — обнови его `README.md` тем же ходом.

---

## 6. Фазы. От нулевого риска к настоящему

### Фаза 0 — предусловия (без них не начинать)

1. Второе окно закончило работу в `direct/`, `git status` чист.
2. Метка отката: `git -C direct tag pre-restructure-20260729 && git log -1 --oneline`
3. Живой смоук ДО: `curl -sS -o /dev/null -w '%{http_code}' https://seoadvanced.ru/direct/automation`
   → записать код. Это baseline, с ним сверяем после каждой фазы.
4. `ssh lxc101 'systemctl is-active direct-create direct-copy direct-content direct-ai
   direct-gateway direct-accounts direct-slepki'` → записать.

---

### Фаза 1 — мусор · риск НУЛЕВОЙ · корень 274 → ~240

Ни один импорт не затрагивается.

```bash
cd home/seoadvanced/direct
# 1. AppleDouble-артефакты macOS (19 шт, уже в .gitignore, в git их нет)
find . -maxdepth 1 -name '._*' -delete
rm -f .DS_Store

# 2. отработавшие логи и probe-артефакты
mkdir -p var
git mv _grind_pass.log _probe_kry_mono2.log _probe_mono.log _probe_pavlov_mb.log var/ 2>/dev/null || mv _grind_pass.log _probe_*.log var/
mv _probe_result_*.json _tone_baseline_result.json var/
mv direct.db slepki_edits_audit.jsonl slepki_pack_cache.marker var/
mv .bak_tp67names_v2_20260713 var/

# 3. var/ в .gitignore
echo 'var/' >> .gitignore
```

⚠️ `direct.db` — проверить `grep -rn 'direct.db' --include='*.py' .` ДО переноса.
Если на него ссылается код — не двигать, а внести путь через переменную.

**Проверка:** `ls | wc -l`, смоук `/direct/automation` = baseline.

---

### Фаза 2 — доки · риск НИЗКИЙ · корень ~240 → ~200

Код не затрагивается. Ломаются только ссылки между доками.

```bash
mkdir -p docs/archive docs/proposals
git mv STATE_ARCHIVE.md DOD_ARCHIVE.md README_ARCHIVE.md ERRORS_JOURNAL_ARCHIVE.md \
       ARCHITECTURE_AUDIT_2026-07-12.md EXTRACTION_PLAN.md docs/archive/
git mv _PROPOSAL_*.md _proposed_piterkina_delete.md docs/proposals/
git mv ARCHITECTURE.md UI_MAP.md ERRORS_JOURNAL.md docs/
```

Остальные доки переезжают вместе со своим пакетом в фазах 3+.

**Проверка — обязательная:** найти битые ссылки между доками
```bash
grep -rnoE '\]\([A-Z_0-9-]+\.md' --include='*.md' . | sort -u
```
каждую проверить `ls`. Плюс `grep -rn '<имя>.md' --include='*.py' .` — доки бывают
зашиты в код (например, читаются в UI).

---

### Фаза 3+ — код, ПО ОДНОМУ пакету за раз

Порядок по возрастанию связности: `copy` → `repair` → `pricing` → `util` → `content` →
`slepki_code` → `create` → `campaign` → `clients` → `web` → `core`.

`core` последним: от него зависят все.

#### Механика одного пакета (пример: `copy/`)

```bash
# 0. ПАУЗА MUTAGEN — иначе прод увидит половину переезда
mutagen sync list                      # узнать имя сессии
mutagen sync pause <session>

# 1. переезд с сохранением истории
mkdir -p copy && touch copy/__init__.py
git mv copy_engine.py copy_steps.py ... copy/      # ВСЕ copy_*, КРОМЕ copy_main.py

# 2. shim'ы в direct/__init__.py — держат `from direct import copy_engine`
cat >> __init__.py <<'EOF'
# --- shim после переезда copy/ (удалить, когда все импорты переписаны) ---
from direct.copy import engine as copy_engine      # noqa: F401
EOF

# 3. прямые импорты `from direct.copy_X` → `from direct.copy.X`
grep -rln 'from direct\.copy_' --include='*.py' . | \
  xargs sed -i '' -E 's/from direct\.copy_([a-z_]+)/from direct.copy.\1/g'

# 4. компиляция ВСЕГО пакета — ловит опечатки, но НЕ ловит мины M1-M4
python3 -m compileall -q . && echo COMPILE_OK

# 5. мины — руками, компилятор их не видит
grep -rn 'importlib\|spec_from_file_location\|__import__' --include='*.py' copy/

# 6. тесты
python3 -m pytest tests/ -x -q

# 7. снять паузу, дождаться синка
mutagen sync resume <session> && mutagen sync flush <session>

# 8. рестарт ТОЛЬКО затронутого юнита
ssh lxc101 'systemctl restart direct-copy && sleep 3 && systemctl is-active direct-copy'
ssh lxc101 'journalctl -u direct-copy -n 40 --no-pager | grep -i "error\|traceback"'

# 9. смоук + git
curl -sS -o /dev/null -w '%{http_code}\n' https://seoadvanced.ru/direct/automation
git add -A && git commit -m "refactor(copy): 35 файлов copy_* → пакет copy/"
```

**Критерий «пакет переехал»:** `compileall` OK · `pytest` OK · юнит `active` ·
в journalctl нет traceback · смоук = baseline · `README.md` пакета написан.

Не сошлось — `git reset --hard` на тег и разбираться. Не чинить на живом.

#### Отдельно про мины при переезде

- **M1 (`routes_autorules.py`)** — до переезда `autorules` выписать реестр сенсоров
  (откуда берётся `module_path`), после — прогнать каждый сенсор руками.
- **M2 (`copy_engine.py:69`)** — путь в `spec_from_file_location` править вручную,
  `sed` его не возьмёт.
- **M3** — после переезда `content/` проверить, что `importlib.reload(text_norm)`
  всё ещё резолвится: короткий скрипт, который импортирует и релоадит.

---

### Фаза 9 — чистка shim'ов (отдельной задачей, спустя время)

Когда всё переехало и неделю проработало:
1. Переписать `from direct import X` → `from direct.<pkg> import X` (112 мест, механически).
2. Удалить shim'ы из `__init__.py`.
3. `compileall` + `pytest` + рестарт всех 12 юнитов + смоук.

Спешить некуда: shim'ы стоят одну строку и ничего не ломают.

---

## 7. Откат

| Что сломалось | Действие |
|---|---|
| Локально, до синка | `git reset --hard pre-restructure-20260729` |
| Уже уехало на LXC 101 | `mutagen sync pause` → `git reset --hard <тег>` → `resume` → `flush` → рестарт всех юнитов |
| Юнит не поднялся | `journalctl -u <unit> -n 100` → чаще всего это мина M1-M4, а не импорт |

Тег `pre-restructure-20260729` не удалять до конца фазы 9.

---

## 8. Оценка

| Фаза | Файлов | Риск | Откат |
|---|---|---|---|
| 1 — мусор | ~35 | нулевой | `rm` не нужен, файлы мусорные |
| 2 — доки | ~13 | низкий | `git mv` обратно |
| 3-8 — код | 184 | средний, управляемый | тег + пауза Mutagen |
| 9 — shim'ы | 112 импортов | низкий | shim вернуть строкой |

Фазы 1-2 можно делать **прямо сейчас**, они не касаются кода и не конфликтуют
со вторым окном (оно правит `.py`, не доки-архивы).
Фазы 3+ — только когда `git status` в `direct/` чист.

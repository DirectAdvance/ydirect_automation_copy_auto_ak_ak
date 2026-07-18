# systemd drop-in overrides (источник правды конфига сплита)

Эти drop-in'ы — **реальный источник** переменных окружения на проде (LXC 101), а не
`Environment=` в главных unit-файлах. Drop-in'ы аддитивны и перекрывают главный юнит.

## Что чем управляет

| Drop-in | Переменная | Зачем |
|---|---|---|
| `direct-create.service.d/role.conf` | `DIRECT_ROLE=web` | **Сплит web/worker**: direct-create.service обслуживает только UI, создание РК — в direct-create-worker.service. Рестарт UI не убивает наборы в работе. |
| `direct-create.service.d/copy.conf` | `DIRECT_REGISTER_COPY=0` | Копирование вынесено в direct-copy.service. |
| `direct-create.service.d/ai.conf` | `DIRECT_REGISTER_AI=0` | API «Обучение ИИ» (`/direct/api/ai/*`) вынесен в direct-ai.service (:5026): вызовы M3/LLM долгие и блокирующие, не должны занимать воркеров создания РК. ⚠️ Ставить **только после** того, как direct-ai.service поднят и nginx уводит на него `/direct/api/ai/` — иначе роуты не обслуживает никто (404). |
| `direct-create.service.d/flags.conf` · `direct-create-worker.service.d/flags.conf` | `DIRECT_PARALLEL_CHANNELS=1`, `DIRECT_API_FIRST=1`, `DIRECT_CONTENT_REUSE_ACCOUNT=1` | Флаги параллельного создания и переиспользования аккаунта под контент. |
| `direct-create.service.d/reuse.conf` · `direct-create-worker.service.d/reuse.conf` | `DIRECT_SITELINK_REUSE_ACCOUNT=1` | Переиспользование аккаунта для быстрых ссылок. |
| `direct-create.service.d/slepki.conf` | `DIRECT_REGISTER_SLEPKI=0` | Редактор слепков вынесен в direct-slepki.service. |
| `direct-create.service.d/neuro-local.conf` · `direct-create-worker.service.d/neuro-local.conf` · `direct-content.service.d/neuro-local.conf` · `direct-content-worker.service.d/neuro-local.conf` · `direct-copy.service.d/neuro-local.conf` | `NEURO_PACK_MOUNT=/opt/neuro_content_local` | Путь к M3 контент-паку. |
| `direct-create.service.d/openrouter-model.conf` · `direct-create-worker.service.d/openrouter-model.conf` · `direct-content.service.d/openrouter-model.conf` | `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` | Модель LLM для контента. |
| `direct-content.service.d/role.conf` | `DIRECT_ROLE=web` | direct-content.service обслуживает UI редактора контента; очередь — в direct-content-worker.service. |
| `direct-copy.service.d/role.conf` | `DIRECT_ROLE=all` | Постановка copy-джобы в in-memory очередь СВОЕГО процесса. При `web` джобу забирал direct-create-worker (чужой процесс) со своим кэшем `direct_copy` и без обновления статуса → `/api/copy_status` вечно `queued`. |

`direct-create-worker.service.d/` дополнительно получает `DIRECT_ROLE=worker` — оно уже в самом
`deploy/direct-create-worker.service` (Environment=), поэтому отдельного drop-in для роли воркера нет.

## ⚠️ Базовые unit-файлы: прод отстаёт от git

Базовые `deploy/direct-*.service` в git для `create`/`create-worker`/`content`/`copy` **новее**,
чем установленные на проде (LXC 101): в git-версии `direct-create.service` значения
`DIRECT_ROLE=web` и `DIRECT_REGISTER_COPY=0` зашиты прямо в юнит, а на проде base-юнит старый и
эти значения ему добавляют drop-in'ы (`role.conf`, `copy.conf`). Поведение идентично (drop-in'ы
аддитивны и выигрывают), поэтому переустанавливать base на прод не требуется — но при пересборке
контейнера ставить **git-версию** base-юнита, а не снимать её с прода.

`direct-*.service` для `accounts`/`gateway`/`slepki`/`slepki-worker`/`content-worker` — снимок с
прода (2026-07-17): base-юниты этих сервисов раньше в git отсутствовали. Drop-in'ы к ним (где есть)
лежат в соответствующих `*.service.d/`.

## Установка (при пересборке контейнера / новом сервере)

```bash
# для каждого юнита:
mkdir -p /etc/systemd/system/direct-create.service.d /etc/systemd/system/direct-create-worker.service.d
cp deploy/dropins/direct-create.service.d/*.conf        /etc/systemd/system/direct-create.service.d/
cp deploy/dropins/direct-create-worker.service.d/*.conf /etc/systemd/system/direct-create-worker.service.d/
systemctl daemon-reload
# порядок рестарта: сначала worker, потом web
systemctl restart direct-create-worker.service
systemctl restart direct-create.service
```

⚠️ Без `role.conf` сплит схлопнется в single-process (`DIRECT_ROLE=all`) — рестарт UI снова
начнёт убивать создание РК в работе. Это и есть главная причина хранить drop-in'ы в git.

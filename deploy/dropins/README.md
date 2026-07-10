# systemd drop-in overrides (источник правды конфига сплита)

Эти drop-in'ы — **реальный источник** переменных окружения на проде (LXC 101), а не
`Environment=` в главных unit-файлах. Drop-in'ы аддитивны и перекрывают главный юнит.

## Что чем управляет

| Drop-in | Переменная | Зачем |
|---|---|---|
| `direct-create.service.d/role.conf` | `DIRECT_ROLE=web` | **Сплит web/worker**: direct-create.service обслуживает только UI, создание РК — в direct-create-worker.service. Рестарт UI не убивает наборы в работе. |
| `direct-create.service.d/copy.conf` | `DIRECT_REGISTER_COPY=0` | Копирование вынесено в direct-copy.service. |
| `direct-create.service.d/neuro-local.conf` · `direct-create-worker.service.d/neuro-local.conf` | `NEURO_PACK_MOUNT=/opt/neuro_content_local` | Путь к M3 контент-паку. |
| `direct-create.service.d/openrouter-model.conf` · `direct-create-worker.service.d/openrouter-model.conf` | `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` | Модель LLM для контента. |

`direct-create-worker.service.d/` дополнительно получает `DIRECT_ROLE=worker` — оно уже в самом
`deploy/direct-create-worker.service` (Environment=), поэтому отдельного drop-in для роли воркера нет.

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

# systemd drop-in overrides (источник правды конфига сплита)

Эти drop-in'ы — **реальный источник** переменных окружения на проде (LXC 101), а не
`Environment=` в главных unit-файлах. Drop-in'ы аддитивны и перекрывают главный юнит.

## Что чем управляет

| Drop-in | Переменная | Зачем |
|---|---|---|
| `direct.service.d/role.conf` | `DIRECT_ROLE=web` | **Сплит web/worker**: direct.service обслуживает только UI, создание РК — в direct-worker.service. Рестарт UI не убивает наборы в работе. |
| `direct.service.d/copy.conf` | `DIRECT_REGISTER_COPY=0` | Копирование вынесено в direct-copy.service. |
| `direct.service.d/neuro-local.conf` · `direct-worker.service.d/neuro-local.conf` | `NEURO_PACK_MOUNT=/opt/neuro_content_local` | Путь к M3 контент-паку. |
| `direct.service.d/openrouter-model.conf` · `direct-worker.service.d/openrouter-model.conf` | `OPENROUTER_LLM_MODEL=deepseek/deepseek-chat` | Модель LLM для контента. |

`direct-worker.service.d/` дополнительно получает `DIRECT_ROLE=worker` — оно уже в самом
`deploy/direct-worker.service` (Environment=), поэтому отдельного drop-in для роли воркера нет.

## Установка (при пересборке контейнера / новом сервере)

```bash
# для каждого юнита:
mkdir -p /etc/systemd/system/direct.service.d /etc/systemd/system/direct-worker.service.d
cp deploy/dropins/direct.service.d/*.conf        /etc/systemd/system/direct.service.d/
cp deploy/dropins/direct-worker.service.d/*.conf /etc/systemd/system/direct-worker.service.d/
systemctl daemon-reload
# порядок рестарта: сначала worker, потом web
systemctl restart direct-worker.service
systemctl restart direct.service
```

⚠️ Без `role.conf` сплит схлопнется в single-process (`DIRECT_ROLE=all`) — рестарт UI снова
начнёт убивать создание РК в работе. Это и есть главная причина хранить drop-in'ы в git.

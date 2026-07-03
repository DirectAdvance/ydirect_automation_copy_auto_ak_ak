# Content Editor Cookie/Grid Plan

Цель: сделать `/direct/automation/content` рабочим редактором контента без OAuth write.

## Уже есть в коде

- `grid_finalize.GridClient` — общий cookie/Grid клиент, CSRF bootstrap и POST в
  `https://direct.yandex.ru/web-api/grid/api`.
- `GridClient.find_and_replace_text(...)` — cookie/Grid writer для массовой замены текста
  объявлений (`TITLE`, `TITLE_EXTENSION`, `BODY`) через `findAndReplaceText`.
- `GridClient.update_ad_images(..., allow_empty_images=True)` — подтверждённая мутация
  `UpdateAdaptiveTextAds` для full-object adaptive updates.
- `GridClient.add_sitelink_sets(...)` — создание наборов быстрых ссылок через Grid.
- `GridClient.set_campaign_callouts(...)` — узкая Grid-мутация campaign `inheritableCallouts`.
- `grid_read.GridReadClient` — read-only Grid-клиент для счётчиков и enrichment.

## Нужно добавить

1. Cookie-read снимка контента.
   - `load` берёт объявления/ссылки/библиотеку уточнений из v5 read-only.
   - Для callouts usage дополняется Grid query campaigns → `inheritableCallouts{assetValue}`.

2. Cookie-write заголовков и текстов.
   - Используется Grid `findAndReplaceText` по точному списку `adIds`, который был найден
     read-only снимком.
   - `ad_title` → `TITLE`, `ad_title2` → `TITLE_EXTENSION`, `ad_text` → `BODY`.

3. Cookie-write быстрых ссылок.
   - Реализовано через set-level swap: создать новый `SitelinkSet` через Grid
     `AddSitelinkSets`, затем перепривязать затронутые кампании через Grid
     `inheritableSitelinkSet`.
   - `FindAndReplaceText` для быстрых ссылок не используется: на реальных ЕПК аккаунтах
     быстрые ссылки часто являются campaign-level assets, а объявления только наследуют
     `assetValue`; массовый вызов по сотням ad ids нестабилен и может падать 500.

4. Cookie-write уточнений.
   - Реализовано: найти callout ids через Grid.
   - Если нового текста ещё нет в библиотеке аккаунта и Grid-схема не поддерживает
     `AddCallouts`, новый CALLOUT создаётся через официальный `adextensions.add`.
   - Перед созданием CALLOUT текст нормализуется до безопасного набора символов Direct
     (например, точки и восклицательные знаки убираются).
   - Реализовано: для каждой затронутой кампании заменить старый id на новый в
     `campaign.inheritableCallouts` через `GridClient.set_campaign_callouts`.

5. Live smoke.
   - Тестовый аккаунт: `porg-psm5h7q6`.
   - Проверено без изменения контента: no-match `findAndReplaceText` на реальном `adId`
     не меняет текст в повторном `/load`; `successCount` у Grid означает обработанные
     объявления, а не гарантированное количество изменённых текстовых значений.
   - Требуется ручной live smoke с тестовым фрагментом: заменить уникальный текст в одном draft/ad,
     затем вернуть назад.

## Запрещено

- `ads.update`, `sitelinks.add`, `campaigns.update` через OAuth v5/v501 для записи.
- Тихий fallback на OAuth write при ошибке cookies/Grid.
- Full-object campaign update для текстовых правок объявлений.

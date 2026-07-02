# Content Editor Cookie/Grid Plan

Цель: сделать `/direct/automation/content` рабочим редактором контента без OAuth write.

## Уже есть в коде

- `grid_finalize.GridClient` — общий cookie/Grid клиент, CSRF bootstrap и POST в
  `https://direct.yandex.ru/web-api/grid/api`.
- `GridClient.update_ad_images(..., allow_empty_images=True)` — подтверждённая мутация
  `UpdateAdaptiveTextAds`, которую можно расширить для текстовых правок adaptive ads.
- `GridClient.add_sitelink_sets(...)` — создание наборов быстрых ссылок через Grid.
- `GridClient.set_campaign_callouts(...)` — узкая Grid-мутация campaign `inheritableCallouts`.
- `grid_read.GridReadClient` — read-only Grid-клиент для счётчиков и enrichment.

## Нужно добавить

1. Cookie-read снимка контента.
   - Grid query `campaigns/adGroups/ads` по `ulogin`.
   - Для adaptive ads читать `id`, `campaignId`, `adGroupId`, `href`, `titles`, `bodies`,
     `imageHashes`, `adPrice`, `inheritableSitelinkSet`.
   - Для sitelinks читать set id и items.
   - Для callouts читать library ids/text и campaign inheritance.

2. Cookie-write заголовков и текстов.
   - Использовать `UpdateAdaptiveTextAds`.
   - Делать read-modify-write полного объекта объявления, не теряя `href`, images, price,
     sitelinks/callouts inheritance.
   - Менять только совпадающее поле `old_text -> new_text`.

3. Cookie-write быстрых ссылок.
   - Создать новый set через Grid `AddSitelinkSets`.
   - Переназначить объявления на новый set через `UpdateAdaptiveTextAds` или узкую mutation,
     подтверждённую HAR.

4. Cookie-write уточнений.
   - Найти/создать callout ids через Grid.
   - Обновить `campaign.inheritableCallouts` через `GridClient.set_campaign_callouts`.

5. Live smoke.
   - Тестовый аккаунт: `porg-psm5h7q6`.
   - Минимальная операция: заменить уникальный тестовый фрагмент в одном draft/ad, затем вернуть назад.
   - Проверить через UI и через cookie-read снимок.

## Запрещено

- `ads.update`, `sitelinks.add`, `campaigns.update` через OAuth v5/v501 для записи.
- Тихий fallback на OAuth write при ошибке cookies/Grid.
- Full-object campaign update для текстовых правок объявлений.


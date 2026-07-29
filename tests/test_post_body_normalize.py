"""Тесты нормализации тела поста посевов (правила 1-11).

Проверяет normalize_post_body_text и вспомогательные функции из create_set_tp8_10.
Все правила согласованы Семёном; изменять без его явного согласования нельзя.
"""

import pytest

from direct.create.create_set_tp8_10 import (
    normalize_post_body_text,
    _normalize_post_body_line,
    _norm_apply_currency,
)


# ── Rule 1: Убрать эмодзи ───────────────────────────────────────────────────

class TestRule1Emoji:
    def test_emoji_removed_from_line_start(self):
        result = _normalize_post_body_line("💰Выгода от автосалона")
        assert result == "Выгода от автосалона"

    def test_emoji_removed_with_space_after(self):
        result = _normalize_post_body_line("🚗 Автокредит на лучших условиях")
        assert result == "Автокредит на лучших условиях"

    def test_checkmark_emoji_removed(self):
        result = _normalize_post_body_line("✅ Простые шаги")
        assert result == "Простые шаги"

    def test_lightning_emoji_removed(self):
        result = _normalize_post_body_line("⚡Ликвидация автопарка")
        assert result == "Ликвидация автопарка"

    def test_heart_with_variation_selector_removed(self):
        # ❤️ = U+2764 + U+FE0F
        result = _normalize_post_body_line("❤️ Акционные предложения")
        assert result == "Акционные предложения"

    def test_leading_space_removed_after_emoji(self):
        # emoji + space → space remains → lstrip removes it
        result = _normalize_post_body_line("👍 Проверенное качество")
        assert result == "Проверенное качество"

    def test_no_emoji_no_change(self):
        line = "Обычный текст без эмодзи"
        result = _normalize_post_body_line(line)
        assert result == line

    def test_emoji_in_full_text(self):
        text = "💰Выгода!\n\n✅ Шаги:\n— Зайди"
        result = normalize_post_body_text(text)
        assert "💰" not in result
        assert "✅" not in result
        assert "Выгода!" in result
        assert "Шаги:" in result


# ── Rule 2: Схлопнуть 2+ пробелов/табов внутри строки ───────────────────────

class TestRule2SqueezeSpaces:
    def test_double_space_squeezed(self):
        result = _normalize_post_body_line("скидка до  500 000 ₽")
        assert result == "скидка до 500 000 ₽"

    def test_triple_space_squeezed(self):
        result = _normalize_post_body_line("от   10 000 ₽/мес")
        assert result == "от 10 000 ₽/мес"

    def test_tab_squeezed(self):
        result = _normalize_post_body_line("первый\t\tвзнос")
        assert result == "первый взнос"

    def test_single_space_preserved(self):
        line = "Один пробел нормально"
        assert _normalize_post_body_line(line) == line


# ── Rule 3: Валюта и процент ─────────────────────────────────────────────────

class TestRule3CurrencyPercent:
    def test_space_added_before_rub(self):
        result = _norm_apply_currency("500 000₽")
        assert result == "500 000 ₽"

    def test_existing_space_before_rub_preserved(self):
        result = _norm_apply_currency("500 000 ₽")
        assert result == "500 000 ₽"

    def test_percent_space_removed(self):
        result = _norm_apply_currency("30 %")
        assert result == "30%"

    def test_percent_no_space_preserved(self):
        result = _norm_apply_currency("30%")
        assert result == "30%"

    def test_mixed_currency_percent(self):
        result = _norm_apply_currency("скидка до 500 000₽, ставка 9 %")
        assert result == "скидка до 500 000 ₽, ставка 9%"


# ── Rule 4: Период платежа → ₽/мес ─────────────────────────────────────────

class TestRule4PaymentPeriod:
    def test_rub_slash_space_mes(self):
        result = _norm_apply_currency("от 5 500 ₽ / мес")
        assert result == "от 5 500 ₽/мес"

    def test_rub_slash_mes_no_space(self):
        result = _norm_apply_currency("10 000 ₽/мес")
        assert result == "10 000 ₽/мес"

    def test_rub_slash_mesyats(self):
        result = _norm_apply_currency("платёж 8 000 ₽ / месяц")
        assert result == "платёж 8 000 ₽/мес"

    def test_rub_slash_mesyats_no_space(self):
        result = _norm_apply_currency("8 000 ₽/месяц")
        assert result == "8 000 ₽/мес"

    def test_rub_v_mesyats(self):
        result = _norm_apply_currency("от 10 000 ₽ в месяц")
        assert result == "от 10 000 ₽/мес"

    def test_rub_nospace_slash_space_mes(self):
        # 500₽ / мес → after rule3: 500 ₽ / мес → rule4: 500 ₽/мес
        result = _norm_apply_currency("500₽ / мес")
        assert result == "500 ₽/мес"

    def test_rub_space_slash_nospace_mes(self):
        result = _norm_apply_currency("5 000 ₽ /мес")
        assert result == "5 000 ₽/мес"


# ── Rule 5: Маркер буллета → "— " ───────────────────────────────────────────

class TestRule5Bullet:
    def test_dash_to_emdash(self):
        result = _normalize_post_body_line("- КАСКО в подарок")
        assert result == "— КАСКО в подарок"

    def test_emdash_preserved(self):
        result = _normalize_post_body_line("— КАСКО в подарок")
        assert result == "— КАСКО в подарок"

    def test_endash_to_emdash(self):
        result = _normalize_post_body_line("– Трейд-ин принимаем")
        assert result == "— Трейд-ин принимаем"

    def test_bullet_dot_to_emdash(self):
        result = _normalize_post_body_line("• Гарантия 12 месяцев")
        assert result == "— Гарантия 12 месяцев"

    def test_asterisk_to_emdash(self):
        result = _normalize_post_body_line("* Бесплатное ТО")
        assert result == "— Бесплатное ТО"

    def test_bullet_with_extra_space_normalized(self):
        result = _normalize_post_body_line("—  Одобрение за 45 минут")
        assert result == "— Одобрение за 45 минут"


# ── Rule 6: Маркер шага → "-> " ─────────────────────────────────────────────

class TestRule6Step:
    def test_arrow_ascii_kept(self):
        result = _normalize_post_body_line("-> Зайди на сайт")
        assert result == "-> Зайди на сайт"

    def test_double_arrow_normalized(self):
        result = _normalize_post_body_line("--> Выбери авто")
        assert result == "-> Выбери авто"

    def test_unicode_arrow_normalized(self):
        result = _normalize_post_body_line("→ Оставь заявку")
        assert result == "-> Оставь заявку"

    def test_fat_arrow_normalized(self):
        result = _normalize_post_body_line("=> Получи звонок")
        assert result == "-> Получи звонок"

    def test_step_no_space_arrow_normalized(self):
        result = _normalize_post_body_line("->Зайди на сайт")
        assert result == "-> Зайди на сайт"


# ── Rule 7: Первая буква тела буллета/шага — прописная ──────────────────────

class TestRule7Uppercase:
    def test_bullet_first_letter_uppercased(self):
        result = _normalize_post_body_line("— все авто проходят диагностику")
        assert result == "— Все авто проходят диагностику"

    def test_step_first_letter_uppercased(self):
        result = _normalize_post_body_line("-> зайди на сайт")
        assert result == "-> Зайди на сайт"

    def test_already_uppercase_no_change(self):
        result = _normalize_post_body_line("— КАСКО в подарок")
        assert result == "— КАСКО в подарок"

    def test_regular_line_not_uppercased(self):
        # Regular lines don't get capitalized
        result = _normalize_post_body_line("первый взнос 0 ₽")
        assert result == "первый взнос 0 ₽"


# ── Rule 8: Хвостовые .,; у буллетов/шагов срезаются, ! ? сохраняются ───────

class TestRule8TrailingPunctuation:
    def test_trailing_comma_removed(self):
        result = _normalize_post_body_line("— КАСКО в подарок,")
        assert result == "— КАСКО в подарок"

    def test_trailing_period_removed(self):
        result = _normalize_post_body_line("— Бесплатное ТО.")
        assert result == "— Бесплатное ТО"

    def test_trailing_semicolon_removed(self):
        result = _normalize_post_body_line("— 12 месяцев гарантии;")
        assert result == "— 12 месяцев гарантии"

    def test_exclamation_preserved(self):
        result = _normalize_post_body_line("— Только до воскресенья!")
        assert result == "— Только до воскресенья!"

    def test_question_preserved(self):
        result = _normalize_post_body_line("-> Уже выбрали авто?")
        assert result == "-> Уже выбрали авто?"

    def test_step_trailing_period_removed(self):
        result = _normalize_post_body_line("-> Забронируй со скидкой.")
        assert result == "-> Забронируй со скидкой"

    def test_regular_line_trailing_period_preserved(self):
        """Правило 8 срезает хвостовые .,; ТОЛЬКО у буллетов/шагов, не у обычных строк."""
        result = _normalize_post_body_line("первый взнос 0 ₽.")
        assert result == "первый взнос 0 ₽."


# ── Rule 9: Убрать пробел перед : ───────────────────────────────────────────

class TestRule9SpaceBeforeColon:
    def test_space_before_colon_removed(self):
        result = _normalize_post_body_line("Проверенное качество :")
        assert result == "Проверенное качество:"

    def test_markup_token_not_touched(self):
        # " :b:" — пробел перед :b: НЕ должен убираться (пробел должен сохраниться)
        result = _normalize_post_body_line("слово :b:текст:bb:")
        assert " :b:" in result

    def test_markup_bb_not_touched(self):
        result = _normalize_post_body_line("слово :bb:")
        assert " :bb:" in result

    def test_colon_at_end_no_space_no_change(self):
        result = _normalize_post_body_line("При оформлении:")
        assert result == "При оформлении:"


# ── Rule 10: Удалить «Подробности по телефону» без цифр ─────────────────────

class TestRule10DanglingPhone:
    def test_phone_line_without_digits_dropped(self):
        text = "Текст.\n\nПодробности по телефону"
        result = normalize_post_body_text(text)
        assert "Подробности по телефону" not in result

    def test_phone_line_with_digits_kept(self):
        text = "Текст.\n\nПодробности по телефону +78612305079"
        result = normalize_post_body_text(text)
        assert "Подробности по телефону" in result

    def test_phone_line_with_colon_and_digits_kept(self):
        text = "Текст.\n\nПодробности по телефону: +79999999991"
        result = normalize_post_body_text(text)
        assert "Подробности по телефону: +79999999991" in result

    def test_phone_line_trailing_space_without_digits_dropped(self):
        text = "Текст.\n\nПодробности по телефону   "
        result = normalize_post_body_text(text)
        assert "Подробности по телефону" not in result


# ── Rule 11: 3+ переводов строки → 2, strip краёв ───────────────────────────

class TestRule11Newlines:
    def test_triple_newline_collapsed(self):
        text = "Первый блок.\n\n\nВторой блок."
        result = normalize_post_body_text(text)
        assert "\n\n\n" not in result
        assert "Первый блок." in result
        assert "Второй блок." in result

    def test_four_newlines_collapsed(self):
        text = "А.\n\n\n\nБ."
        result = normalize_post_body_text(text)
        assert result == "А.\n\nБ."

    def test_leading_trailing_stripped(self):
        text = "\n\nТекст.\n\n"
        result = normalize_post_body_text(text)
        assert result == "Текст."

    def test_two_newlines_preserved(self):
        text = "Блок 1.\n\nБлок 2."
        result = normalize_post_body_text(text)
        assert "Блок 1.\n\nБлок 2." in result


# ── Идемпотентность ─────────────────────────────────────────────────────────

class TestIdempotency:
    SAMPLES = [
        "Выгода от автосалона: скидка до 500 000 ₽\n\n— КАСКО в подарок\n— Первый взнос 0%",
        "Автокредит:\n— Платёж от 10 000 ₽/мес\n— Одобрение за 45 минут\n\n-> Зайди\n-> Выбери\n-> Забронируй",
        ":b:При оформлении::bb:\n— КАСКО\n— Шины",
        "Текст без особого форматирования. Всё нормально.",
    ]

    @pytest.mark.parametrize("sample", SAMPLES)
    def test_idempotent(self, sample):
        once = normalize_post_body_text(sample)
        twice = normalize_post_body_text(once)
        assert once == twice, f"Не идемпотентно!\nПосле 1 раза: {once!r}\nПосле 2 раз: {twice!r}"


# ── Markup-токены сохраняются ────────────────────────────────────────────────

class TestMarkupPreserved:
    def test_bold_markup_preserved(self):
        text = ":b:Haval Jolion:bb: — от 7 355 ₽/мес"
        result = normalize_post_body_text(text)
        assert ":b:Haval Jolion:bb:" in result

    def test_italic_markup_preserved(self):
        text = ":i:Дилер освобождает склады.:ii:\n\nВ наличии."
        result = normalize_post_body_text(text)
        assert ":i:Дилер освобождает склады.:ii:" in result

    def test_markup_line_not_treated_as_bullet(self):
        # Строка начинается с :b: — не должна быть распознана как буллет
        text = ":b:При оформлении::bb:"
        result = normalize_post_body_text(text)
        # Должна остаться как есть, а не стать "— При оформлении:"
        assert result == ":b:При оформлении::bb:"

    def test_inline_bold_in_bullet_preserved(self):
        # Буллет с inline bold
        text = "— :b:КАСКО в подарок:bb:"
        result = normalize_post_body_text(text)
        # Маркер нормализован, inline markup сохранён
        assert ":b:КАСКО в подарок:bb:" in result

    def test_strike_markup_preserved(self):
        text = "Старая цена :s:1 200 000:ss: ₽ — теперь 900 000 ₽"
        result = normalize_post_body_text(text)
        assert ":s:1 200 000:ss:" in result


# ── Интеграционный тест на образце из прототипа ──────────────────────────────

class TestIntegration:
    def test_full_sample_normalization(self):
        """Образец из norm_proto.py: комплексный текст с эмодзи, буллетами, шагами."""
        src = (
            "Огромный выбор автомобилей с пробегом!\n"
            "В наличии более 5000 подержанных машин под любой бюджет.\n"
            "\n"
            "\U0001F4B0Выгода от автосалона: скидка до 500 000₽ + 2-й  комплект резины в подарок!\n"
            "\n"
            "\U0001F44CПроверенное качество:\n"
            "— все авто проходят полную диагностику,\n"
            "— 12 месяцев гарантии + бесплатное ТО.\n"
            "\n"
            "\U0001F697 Автокредит на лучших условиях:\n"
            "— 0% первоначальный взнос.\n"
            "— Одобрение кредита за 45 минут.\n"
            "—  Платеж от 10 000 ₽ / месяц\n"
            "\n"
            "✅ Простые шаги:\n"
            "-> зайди на сайт\n"
            "-> выбери авто\n"
            "-> забронируй его с персональной скидкой\n"
            "\n"
            "Не упусти шанс. Количество акционных машин ограничено"
        )
        result = normalize_post_body_text(src)

        # Эмодзи убраны
        assert "\U0001F4B0" not in result
        assert "\U0001F44C" not in result
        assert "\U0001F697" not in result
        assert "✅" not in result

        # Двойные пробелы убраны
        assert "  " not in result

        # Валюта: пробел перед ₽
        assert "500 000 ₽" in result

        # Rule 4: период платежа
        assert "₽/мес" in result
        assert "₽ / месяц" not in result

        # Буллеты нормализованы (строчные → заглавные, .,; убраны)
        assert "— Все авто проходят полную диагностику" in result
        assert "— 12 месяцев гарантии + бесплатное ТО" in result

        # Шаги нормализованы
        assert "-> Зайди на сайт" in result
        assert "-> Выбери авто" in result
        assert "-> Забронируй его с персональной скидкой" in result

    def test_dangling_phone_sample(self):
        """Образец с оборванной строкой телефона (без цифр)."""
        src = "Текст акции.\n\nПодробности по телефону"
        result = normalize_post_body_text(src)
        assert "Подробности по телефону" not in result
        assert "Текст акции." in result

    def test_real_phone_preserved(self):
        """Строка с номером — должна остаться."""
        src = "Текст акции.\n\nПодробности по телефону +78612305079"
        result = normalize_post_body_text(src)
        assert "Подробности по телефону +78612305079" in result

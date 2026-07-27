"""Тесты детерминированного блока марочных оферов для посевов (tp8/tp9/tp10).

Проверяет _build_brand_offer_block, _replace_post_model_list, _fmt_payment,
_BRAND_OFFER_DIVISOR из create_set_tp8_10.
Правила согласованы Семёном; изменять без его явного согласования нельзя.
"""

import re

from direct.create_set_tp8_10 import (
    _build_brand_offer_block,
    _replace_post_model_list,
    _fmt_payment,
    _BRAND_OFFER_DIVISOR,
    normalize_post_body_text,
)


# ── Вспомогательные заглушки ─────────────────────────────────────────────────

class _MockDeps:
    """Контекст-менеджер: временно подставляет DEPS-функции в create_set_tp8_10."""
    def __init__(self, deps: dict):
        from direct import create_set_tp8_10 as m
        self._module = m
        self._deps = deps
        self._orig = {}

    def __enter__(self):
        from direct import create_set_tp8_10 as m
        for k, v in self._deps.items():
            self._orig[k] = m._DEPS.get(k)
            m._DEPS[k] = v
        return self

    def __exit__(self, *_):
        from direct import create_set_tp8_10 as m
        for k, v in self._orig.items():
            if v is None:
                m._DEPS.pop(k, None)
            else:
                m._DEPS[k] = v


MOCK_PRICES = {
    # Haval brand key: cheapest Haval model
    "haval": (876_000, 1_100_000),
    # Haval model keys
    "haval jolion": (876_000, 1_100_000),     # payment = 876000 // 84 = 10428
    "haval f7": (1_050_720, 1_300_000),       # payment = 1050720 // 84 = 12508
    "haval h6": (1_176_000, 1_500_000),       # payment = 1176000 // 84 = 14000
    "haval dargo": (1_512_000, 1_800_000),    # payment = 1512000 // 84 = 18000
    "haval jolion ev": (1_848_000, 2_200_000), # payment = 1848000 // 84 = 22000
    # Non-Haval brand (should be ignored for brand-level Haval campaign)
    "lada vesta": (800_000, 1_000_000),
}

MOCK_URLS = {
    "haval jolion": "https://test.site/auto/haval/jolion",
    "haval f7":     "https://test.site/auto/haval/f7",
    "haval h6":     "https://test.site/auto/haval/h6",
}


def _make_prices_fn(prices: dict):
    def fn(login: str, url: str = "") -> dict:
        return prices
    return fn


def _make_urls_fn(urls: dict):
    def fn(login: str, url: str = "") -> dict:
        return urls
    return fn


def _make_ct_segment_fn(mapping: dict):
    def fn(ct: str) -> str:
        return mapping.get(ct, "")
    return fn


# Стандартный контекст для Haval марочной кампании
BRAND_DEPS = {
    "_account_offer_prices": _make_prices_fn(MOCK_PRICES),
    "_account_offer_urls": _make_urls_fn(MOCK_URLS),
    "_ct_segment": _make_ct_segment_fn({"ct0100": "Марки", "ct0001": "Модели", "ct0000": "Общее"}),
}


# ── _fmt_payment ─────────────────────────────────────────────────────────────

class TestFmtPayment:
    def test_thousands_separated(self):
        assert _fmt_payment(7355) == "7 355"

    def test_large_number(self):
        assert _fmt_payment(100000) == "100 000"

    def test_small_number(self):
        assert _fmt_payment(999) == "999"

    def test_million(self):
        assert _fmt_payment(1_000_000) == "1 000 000"

    def test_divisor_constant(self):
        """Делитель 84 зафиксирован: простое деление, без аннуитета/ставки."""
        assert _BRAND_OFFER_DIVISOR == 84

    def test_floor_division(self):
        """Округление вниз: не завышаем платёж в рекламе."""
        payment = 876_000 // _BRAND_OFFER_DIVISOR
        assert payment == 10428  # 876000 / 84 = 10428.57... → floor = 10428


# ── _build_brand_offer_block ─────────────────────────────────────────────────

class TestBuildBrandOfferBlock:
    def test_returns_5_lines_when_enough_data(self):
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        lines = [l for l in result.splitlines() if l.strip()]
        assert len(lines) == 5, f"Ожидали 5 строк, получили {len(lines)}: {result!r}"

    def test_sorted_by_ascending_payment(self):
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        lines = result.splitlines()
        # Извлечь суммы и проверить порядок
        amounts = []
        for line in lines:
            m = re.search(r"от ([\d\s]+) ₽/мес", line)
            if m:
                amounts.append(int(m.group(1).replace(" ", "")))
        assert amounts == sorted(amounts), f"Строки не отсортированы по платежу: {amounts}"

    def test_cheapest_first(self):
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        first_line = result.splitlines()[0]
        # Haval Jolion должен быть первым (платёж 10 428 ₽/мес)
        assert "Jolion" in first_line
        assert "10 428" in first_line

    def test_format_with_endash(self):
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        # Разделитель: пробел + en-dash (U+2013) + пробел, формат «от N ₽/мес»
        for line in result.splitlines():
            assert " – от " in line, f"Нет разделителя ' – от ' в строке: {line!r}"
            assert "₽/мес" in line, f"Нет '₽/мес' в строке: {line!r}"

    def test_no_percent_rate(self):
        """Правило Семёна: % ставки кредита НЕ печатается никогда."""
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        assert "%" not in result, f"Нашли % в блоке: {result!r}"
        assert "%" not in result

    def test_empty_for_non_brand_campaign(self):
        """Для НЕ-марочных кампаний блок НЕ строится."""
        with _MockDeps(BRAND_DEPS):
            result_model = _build_brand_offer_block("test_login", "https://test.site", "Haval Jolion", "ct0001")
            result_common = _build_brand_offer_block("test_login", "https://test.site", "Посевы", "ct0000")
        assert result_model == "", f"Ожидали '' для Модели, получили: {result_model!r}"
        assert result_common == "", f"Ожидали '' для Общего, получили: {result_common!r}"

    def test_empty_for_posev_label(self):
        """Для лейбла 'Посевы' блок не строится."""
        with _MockDeps(BRAND_DEPS):
            result = _build_brand_offer_block("test_login", "https://test.site", "Посевы", "ct0100")
        assert result == ""

    def test_empty_when_no_price_data(self):
        """При отсутствии цен — пустой блок."""
        deps = {**BRAND_DEPS, "_account_offer_prices": _make_prices_fn({})}
        with _MockDeps(deps):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        assert result == ""

    def test_skip_positions_without_valid_price(self):
        """Позиции без валидной цены не попадают в блок."""
        sparse_prices = {
            "haval jolion": (876_000, 1_100_000),   # valid
            "haval f7":     (0, 0),                  # invalid (no price)
            "haval h6":     (1_176_000, 1_500_000),  # valid
        }
        deps = {**BRAND_DEPS, "_account_offer_prices": _make_prices_fn(sparse_prices)}
        with _MockDeps(deps):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        # F7 (цена 0) не должна попасть ни при каком значении платежа
        assert "F7" not in result

    def test_less_than_5_when_feed_has_few(self):
        """Меньше 5 позиций в фиде → выводить сколько есть (не падать)."""
        two_prices = {
            "haval jolion": (876_000, 1_100_000),
            "haval f7":     (1_050_720, 1_300_000),
        }
        deps = {**BRAND_DEPS, "_account_offer_prices": _make_prices_fn(two_prices)}
        with _MockDeps(deps):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        lines = [l for l in result.splitlines() if l.strip()]
        assert 1 <= len(lines) <= 2

    def test_actual_price_used_not_old_price(self):
        """Берётся актуальная (current, новая) цена, НЕ старая."""
        prices = {
            "haval jolion": (876_000, 1_500_000),   # current=876000, old=1500000
        }
        deps = {**BRAND_DEPS, "_account_offer_prices": _make_prices_fn(prices)}
        with _MockDeps(deps):
            result = _build_brand_offer_block("test_login", "https://test.site", "Haval", "ct0100")
        # Платёж от актуальной цены: 876000 // 84 = 10428
        expected_pay = 876_000 // _BRAND_OFFER_DIVISOR
        assert _fmt_payment(expected_pay) in result
        # Платёж от старой цены: 1500000 // 84 = 17857 → НЕ должен быть в блоке
        old_pay = 1_500_000 // _BRAND_OFFER_DIVISOR
        assert _fmt_payment(old_pay) not in result


# ── _replace_post_model_list ─────────────────────────────────────────────────

class TestReplacePostModelList:
    BRAND_BLOCK_5 = (
        "Haval Jolion – от 10 428 ₽/мес\n"
        "Haval F7 – от 12 508 ₽/мес\n"
        "Haval H6 – от 14 000 ₽/мес\n"
        "Haval Dargo – от 18 000 ₽/мес\n"
        "Haval Jolion EV – от 22 000 ₽/мес"
    )

    def test_replaces_price_paragraph(self):
        body = (
            ":i:Дилер освобождает склады.:ii:\n\n"
            "В наличии:\n"
            "— Haval Jolion — от 7 000 ₽/мес\n"
            "— Haval F7 — от 9 000 ₽/мес\n"
            "— Haval H6 — от 11 000 ₽/мес\n\n"
            ":b:Оставьте заявку:bb: — перезвоним!"
        )
        result = _replace_post_model_list(body, self.BRAND_BLOCK_5)
        # Старые фейковые суммы убраны
        assert "7 000 ₽/мес" not in result
        assert "9 000 ₽/мес" not in result
        # Детерминированные суммы присутствуют
        assert "10 428 ₽/мес" in result
        assert "12 508 ₽/мес" in result
        # Заголовок «В наличии:» сохранён
        assert "В наличии:" in result
        # CTA сохранён
        assert "Оставьте заявку" in result

    def test_inserts_after_first_paragraph_if_no_price_section(self):
        body = (
            ":i:Дилер освобождает склады.:ii:\n\n"
            ":b:Оставьте заявку:bb: — перезвоним."
        )
        result = _replace_post_model_list(body, self.BRAND_BLOCK_5)
        assert "10 428 ₽/мес" in result
        assert "Дилер освобождает" in result
        assert "Оставьте заявку" in result

    def test_empty_block_returns_unchanged_body(self):
        body = "Текст без изменений."
        result = _replace_post_model_list(body, "")
        assert result == body

    def test_two_price_lines_trigger_replacement(self):
        """Триггер замены: 2+ строк с ₽/мес в одном параграфе."""
        body = "Вступление.\n\nМодель А – от 1 000 ₽/мес\nМодель Б – от 2 000 ₽/мес\n\nCTA."
        result = _replace_post_model_list(body, "Haval – от 500 ₽/мес")
        assert "Haval – от 500 ₽/мес" in result
        assert "Модель А" not in result
        assert "Модель Б" not in result

    def test_per_paragraph_model_list_replaced(self):
        """Если LLM выдаёт каждую модель ОТДЕЛЬНЫМ параграфом, ценовой блок в посте — ОДИН.

        Дефект до фикса: ни один параграф не набирал 2 ₽/мес-строки → замена не
        срабатывала → детерминированный блок вставлялся после первого параграфа, а
        LLM-суммы на те же машины оставались → два ценовых блока с разными суммами.
        """
        body = (
            "Вступление.\n\n"
            "Haval Jolion – от 7 000 ₽/мес\n\n"
            "Haval F7 – от 9 000 ₽/мес\n\n"
            "Haval H6 – от 11 000 ₽/мес\n\n"
            "CTA."
        )
        result = _replace_post_model_list(body, self.BRAND_BLOCK_5)
        # Детерминированный блок присутствует
        assert "10 428 ₽/мес" in result
        # LLM-суммы удалены
        assert "7 000 ₽/мес" not in result
        assert "9 000 ₽/мес" not in result
        assert "11 000 ₽/мес" not in result
        # В посте ровно ОДИН набор ценовых строк: детерминированный
        import re as _re
        price_lines = [ln for ln in result.splitlines() if _re.search(r"₽/мес", ln)]
        # Все ценовые строки должны принадлежать brand_block (5 строк)
        assert len(price_lines) == 5, (
            f"Ожидали 5 ценовых строк (brand_block), нашли {len(price_lines)}:\n{result}"
        )
        # Вступление и CTA сохранены
        assert "Вступление." in result
        assert "CTA." in result

    def test_one_price_line_not_replaced(self):
        """Одна строка с ₽/мес — не является блоком списка (total < 2), замена не происходит.

        Сосуществование одиночного кредитного упоминания («платёж от X ₽/мес»)
        и детерминированного блока допустимо: первое — общая кредитная фраза,
        второе — конкретные модели со своими суммами. Конфликта нет, потому что
        это разные смысловые блоки. Дефект возникает только при total >= 2, когда
        LLM-суммы на ОДНИ И ТЕ ЖЕ модели конкурируют с детерминированными.
        """
        body = "Платёж от 10 000 ₽/мес — доступно каждому.\n\nCTA."
        result = _replace_post_model_list(body, "Haval – от 500 ₽/мес")
        # Детерминированный блок вставляется после первого параграфа (список не обнаружен)
        assert "Haval – от 500 ₽/мес" in result
        # Одиночное кредитное упоминание сохранено
        assert "10 000 ₽/мес" in result


# ── Идемпотентность блока с нормализацией ────────────────────────────────────

class TestBrandBlockIdempotency:
    SAMPLE_LINES = [
        "Haval Jolion – от 10 428 ₽/мес",
        "Haval F7 – от 12 508 ₽/мес",
        "Haval H6 – от 14 000 ₽/мес",
        "Haval Dargo – от 18 000 ₽/мес",
        "Haval Jolion EV – от 22 000 ₽/мес",
    ]

    def test_each_line_idempotent_with_normalize(self):
        """Строки блока проходят normalize_post_body_text без изменений."""
        from direct.create_set_tp8_10 import _normalize_post_body_line
        for line in self.SAMPLE_LINES:
            result = _normalize_post_body_line(line)
            assert result == line, (
                f"Строка блока изменилась при нормализации!\n"
                f"  было: {line!r}\n"
                f"  стало: {result!r}"
            )

    def test_full_block_idempotent(self):
        """Весь блок из 5 строк не меняется при прогоне normalize_post_body_text."""
        block = "\n".join(self.SAMPLE_LINES)
        once = normalize_post_body_text(block)
        twice = normalize_post_body_text(once)
        assert once == block, (
            f"Блок изменился при нормализации!\n"
            f"  было: {block!r}\n"
            f"  стало: {once!r}"
        )
        assert once == twice, "Нормализация не идемпотентна для блока"

    def test_endash_not_treated_as_bullet(self):
        """En-dash в середине строки не путается с буллет-маркером."""
        from direct.create_set_tp8_10 import _normalize_post_body_line
        # «–» в НАЧАЛЕ строки — буллет (правило 5), в СЕРЕДИНЕ — нет
        line = "Haval Jolion – от 10 428 ₽/мес"
        result = _normalize_post_body_line(line)
        assert result == line   # не должно стать «— Haval Jolion – от ...»

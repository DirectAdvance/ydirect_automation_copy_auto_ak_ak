"""Числовая добивка UAC-контента не должна давать бессмысленных склеек.

Боевой дефект (Мастер кампаний, 2026-07-28, скрин Семёна «Варианты текстов объявлений»):
    «Выберите авто и подтвердите удобное время для поездки. Оставьте заявку до 45%»
Причина: числовая «подсказка» бралась regex'ом как ГОЛЫЙ фрагмент («до 45%») и клеилась
через пробел сразу после призыва. Заявка не бывает «до 45%».

Проверяем ровно то, что чинили:
  А. к призыву не цепляется несогласованный числовой обрывок;
  Б. законная добивка (законченный сегмент из своего же контента) продолжает работать —
     отдельным предложением;
  В. не влезло в лимит — строка остаётся КОРОЧЕ, но осмысленной (не обрывок).
"""
from direct.create_set_master_product import (
    _attach_cta_hint,
    _attach_number_clause,
    _pavlov_multibrand_texts,
    _strip_dangling_uac_tail,
    _uac_number_clause,
)

_SRC = [
    "Выгода до 45% на новые авто",
    "Платёж от 9 000 ₽/мес. Одобрение 98%",
]


def test_clause_is_whole_segment_not_bare_fragment():
    assert _uac_number_clause(_SRC) == "Выгода до 45% на новые авто"
    # Сегмент длиннее лимита «хвоста» — берём следующий подходящий, а не режем на обрывок.
    assert _uac_number_clause(["Слишком длинное предложение источника про выгоду до 45% годовых "
                               "и прочие условия покупки"]) == ""


def test_cta_never_gets_incoherent_numeric_tail():
    base = "Выберите авто и подтвердите удобное время для поездки"
    out = _attach_cta_hint(base, "Выгода до 45%", 81)
    assert "Оставьте заявку до 45%" not in out
    # 52 симв. базы: ни «. Выгода до 45%. Оставьте заявку», ни «. Оставьте заявку за 15 минут»
    # в 81 не влезают → отдаём базу целиком, без обрывка (правило: короче, но осмысленно).
    assert out == base
    assert len(out) <= 81


def test_cta_keeps_number_as_separate_sentence_when_it_fits():
    out = _attach_cta_hint("Новые авто в наличии", "Выгода до 45%", 81)
    assert out == "Новые авто в наличии. Выгода до 45%. Оставьте заявку"


def test_cta_time_adverbial_stays_glued_to_call_to_action():
    out = _attach_cta_hint("Новые авто в наличии", "За 15 минут", 81)
    assert out == "Новые авто в наличии. Оставьте заявку за 15 минут"


def test_cta_falls_back_to_shorter_variant_then_to_base():
    long_base = "Б" * 70
    # Полный вариант с сегментом не влезает → берём короткий призыв с обстоятельством.
    assert _attach_cta_hint(long_base, "Выгода до 45%", 100) == f"{long_base}. Оставьте заявку за 15 минут"
    # Не влезает ничего → строка остаётся короче, но осмысленной (без обрывка).
    assert _attach_cta_hint(long_base, "Выгода до 45%", 75) == long_base


def test_number_clause_appended_as_sentence_not_by_space():
    out = _attach_number_clause("Выберите марку в наличии", "Выгода до 45%", 81)
    assert out == "Выберите марку в наличии. Выгода до 45%"
    # Пустая подсказка → законченная фраза-фолбэк, а не голое «за 15 минут».
    assert _attach_number_clause("Выберите марку в наличии", "", 81) == (
        "Выберите марку в наличии. Одобрение за 15 минут")
    # Лимит заголовка не позволяет добить → база без хвоста (number-гейт вызывающего отсеет).
    assert _attach_number_clause("Выберите марку в наличии", "Выгода до 45%", 30) == (
        "Выберите марку в наличии")


def test_dangling_tail_and_capital_first_letter():
    assert _strip_dangling_uac_tail("новое авто в наличии. КАСКО на год") == (
        "Новое авто в наличии. КАСКО на год")
    assert _strip_dangling_uac_tail("Новые авто. Выгода до 900") == "Новые авто"
    assert _strip_dangling_uac_tail("Новые авто. Выгода до") == "Новые авто"
    # Законные концовки не трогаем.
    assert _strip_dangling_uac_tail("Новые авто. Выгода до 45%") == "Новые авто. Выгода до 45%"
    assert _strip_dangling_uac_tail("Тест-драйв за 1 день") == "Тест-драйв за 1 день"
    assert _strip_dangling_uac_tail("Авто в кредит. Одобрение 98%") == "Авто в кредит. Одобрение 98%"


def test_pavlov_multibrand_texts_fit_limit_and_start_capitalized():
    for _t in _pavlov_multibrand_texts():
        assert len(_t) <= 81, (len(_t), _t)
        assert _t[:1] == _t[:1].upper(), _t

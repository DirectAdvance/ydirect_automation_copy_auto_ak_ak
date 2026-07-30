from direct.copy_service.copy_keyword_phrase import clean_keyword_phrase
from direct.copy_service.copy_uac import _copy_uac_keyword_strings, _copy_uac_sanitize_keywords


def test_clean_keyword_phrase_drops_dangling_minus_from_agent_board_76():
    phrase = "купить baic -авто -машина -новый -автомобиль -"

    assert clean_keyword_phrase(phrase) == "купить baic -авто -машина -новый -автомобиль"


def test_clean_keyword_phrase_drops_target_geo_inline_minus_phrase():
    geo_pairs = [("Краснодар", "Нижний Новгород")]

    assert clean_keyword_phrase("купить baic -авто -нижний новгород", geo_pairs) == "купить baic -авто"


def test_uac_keyword_strings_cleans_after_geo_replacement():
    geo_pairs = [("Краснодар", "Нижний Новгород")]

    assert _copy_uac_keyword_strings(["купить baic -авто -краснодар"], geo_pairs) == ["купить baic -авто"]


def test_uac_sanitize_keeps_agent_board_76_phrase_out_of_keywords_validation():
    keywords = _copy_uac_keyword_strings(["купить baic -авто -машина -новый -автомобиль -"], [])

    clean_keywords, minus_keywords = _copy_uac_sanitize_keywords(keywords, [])

    assert clean_keywords == ["купить baic"]
    assert minus_keywords == ["авто", "машина", "новый", "автомобиль"]


def test_uac_keyword_strings_limits_agent_board_77_phrase_after_geo_replacement():
    geo_pairs = [
        ("Краснодар", "Нижний Новгород"),
        ("Краснодарский край", "Нижегородская область"),
    ]

    keywords = _copy_uac_keyword_strings(
        ["авито нижний новгород нижегородская область авто +с пробегом"],
        geo_pairs,
    )

    assert keywords == ["авито авто +с пробегом"]


def test_uac_keyword_strings_limits_overlong_phrase_without_dangling_plus_word():
    keywords = _copy_uac_keyword_strings(
        ["авито нижний новгород нижегородская область авто +с пробегом"],
        [],
    )

    assert keywords == ["авито нижний новгород нижегородская область +с пробегом"]

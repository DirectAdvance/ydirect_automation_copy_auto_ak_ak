"""Лимит «7 слов» в ключевой фразе считается по ПОЗИТИВНЫМ словам, минус-части не в счёт.

Лимит Директа (docs/troubleshooting/interface.md, раздел «Ключевые фразы»):
  • «Количество слов для одной ключевой фразы — не более 7, без учёта стоп-слов»;
  • минус-фразы лимитируются ОТДЕЛЬНО («не более 7» слов на каждую минус-фразу),
    в лимит самой ключевой фразы они не входят;
  • символьный лимит 4096 — наоборот, «включая минус-слова».

Боевой факт 2026-07-28 (ct0010 Drom, слепок scherbakova): `_kw_clean` считал ВСЕ токены,
поэтому легальная фраза с 4 позитивными словами и 13 минус-словами (17 токенов) выбрасывалась
целиком, а вслед за ней — все ключи групп tp1 «Общие - КС» (кампании 713096741 / 713096753).
"""

import importlib.util
import pathlib

from direct import automation_runtime
from direct import create_set_tp1_builders

# Переиспользуем боевой стенд deps для `_build_tp1_adgroups` из соседнего модуля тестов,
# чтобы не дублировать ~85 строк моков Grid/v501.
_HARNESS_PATH = pathlib.Path(__file__).with_name("test_create_auto_regressions.py")
_spec = importlib.util.spec_from_file_location("_tp1_harness_for_kw_clean", _HARNESS_PATH)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
_make_tp1_test_deps = _harness._make_tp1_test_deps


LIVE_PHRASE = ("drom ru продажа авто -запчасти -экзамен -ниссан -договор -крым -уаз -нива "
               "-автозапчасти -амур -амурская -гай -спецтехника -улан")


# ── 1. Сам счётчик / _kw_clean ───────────────────────────────────────────────────────────────

def test_positive_word_count_ignores_minus_parts():
    assert automation_runtime._kw_positive_words(LIVE_PHRASE) == 4
    assert len(LIVE_PHRASE.split()) == 17, "исходная фраза должна быть длиннее 7 токенов целиком"


def test_live_phrase_with_13_minus_words_survives_and_keeps_minus_parts():
    out = automation_runtime._kw_clean([LIVE_PHRASE], 200)

    assert out == [LIVE_PHRASE], f"легальная фраза (4 позитивных слова) отсеяна: {out!r}"
    for minus in ("-запчасти", "-нива", "-улан"):
        assert minus in out[0], f"минус-слово {minus} потерялось из фразы: {out[0]!r}"


def test_nine_positive_words_are_still_dropped():
    long_kw = "купить новый автомобиль в москве недорого с пробегом сегодня"
    assert automation_runtime._kw_positive_words(long_kw) == 9
    assert automation_runtime._kw_clean([long_kw], 200) == [], "реальное превышение обязано отсеиваться"


def test_positive_limit_boundary_seven_passes_eight_fails():
    seven = "один два три четыре пять шесть семь"
    eight = seven + " восемь"
    assert automation_runtime._kw_clean([seven], 200) == [seven]
    assert automation_runtime._kw_clean([eight], 200) == []
    # восемь позитивных не спасаются приписыванием минусов
    assert automation_runtime._kw_clean([eight + " -отзывы"], 200) == []


def test_char_limit_still_counts_minus_words():
    """4096 символов Директ считает ВКЛЮЧАЯ минус-слова — этот лимит не ослабляем."""
    huge = "авто " + " ".join(f"-минус{i}" for i in range(600))
    assert len(huge) > 4096
    assert automation_runtime._kw_clean([huge], 200) == []


# ── 2. Гейт «все фразы отсеялись» в tp1-билдере ──────────────────────────────────────────────

def _run_tp1_with_kw_clean(kw_clean, *, keywords, autotarget, keep_keywords, tp_code="tp1"):
    """Прогон `_build_tp1_adgroups` с подменённым `_kw_clean`; вернуть (rep, kw_calls)."""
    deps, _grid_calls, kw_calls, _ads = _make_tp1_test_deps()
    deps["_kw_clean"] = kw_clean
    create_set_tp1_builders.configure(deps)

    groups = [{"name": "тест-группа", "keywords": list(keywords), "titles": [], "texts": []}]
    rep = create_set_tp1_builders._build_tp1_adgroups(
        token="tok", login="porg-test", campaign_id=999, region_ids=[213],
        href="https://example.com", groups=groups, autotarget=autotarget,
        keep_keywords=keep_keywords, tp_code=tp_code)
    return rep, kw_calls


def test_ks_group_with_all_phrases_filtered_out_reports_visible_error():
    """КС-группа: на входе фразы БЫЛИ, очистка съела все → это видимая ошибка, а не тишина."""
    rep, kw_calls = _run_tp1_with_kw_clean(
        lambda kws, limit: [],                       # очистка отсеяла всё
        keywords=[LIVE_PHRASE, "ещё одна фраза"], autotarget=False, keep_keywords=False)

    assert kw_calls == [], "ключи не отправлялись — именно этот случай и должен быть виден"
    errs = rep.get("errors") or []
    assert any("ключи(tp1)" in str(e) and "отсея" in str(e) for e in errs), (
        f"тихий ноль ключей после очистки обязан попасть в rep['errors'], получили {errs!r}")
    assert any("2 фраз" in str(e) for e in errs), f"в ошибке нет числа входных фраз: {errs!r}"


def test_ks_group_with_surviving_phrases_has_no_such_error():
    """Контроль: фразы пережили очистку → нового сообщения нет (не шумим на здоровом пути)."""
    rep, kw_calls = _run_tp1_with_kw_clean(
        lambda kws, limit: [k for k in (kws or []) if k][:limit],
        keywords=[LIVE_PHRASE], autotarget=False, keep_keywords=False)

    assert [it["keyword"] for batch in kw_calls for it in batch] == [LIVE_PHRASE]
    assert not [e for e in (rep.get("errors") or []) if "отсея" in str(e)], rep.get("errors")


def test_autotarget_group_without_real_keywords_is_not_an_error():
    """Чистый автотаргет (autotarget=True, keep_keywords=False): ключей нет by design."""
    rep, kw_calls = _run_tp1_with_kw_clean(
        lambda kws, limit: [],
        keywords=[LIVE_PHRASE], autotarget=True, keep_keywords=False)

    assert kw_calls == []
    kw_errs = [e for e in (rep.get("errors") or []) if "ключи(" in str(e)]
    assert not kw_errs, (
        f"автотаргет-группа без реальных ключей — норма, ошибки быть не должно: {kw_errs!r}")
    assert not rep.get("error")


def test_tp5_all_phrases_filtered_out_is_fatal():
    """На поисковой tp5 тот же случай обязан дойти до singular rep['error'] (позиция не ok)."""
    rep, _kw_calls = _run_tp1_with_kw_clean(
        lambda kws, limit: [], keywords=[LIVE_PHRASE], autotarget=False,
        keep_keywords=False, tp_code="tp5")

    assert any("ключи(tp5)" in str(e) for e in (rep.get("errors") or []))
    assert rep.get("error"), f"tp5 без ключей — структурный провал; rep={rep!r}"


def test_real_kw_clean_end_to_end_keeps_live_phrase_in_tp1():
    """Сквозной путь с НАСТОЯЩИМ `_kw_clean`: боевая фраза доезжает до AddKeywords."""
    rep, kw_calls = _run_tp1_with_kw_clean(
        automation_runtime._kw_clean,
        keywords=[LIVE_PHRASE], autotarget=False, keep_keywords=False)

    sent = [it["keyword"] for batch in kw_calls for it in batch]
    assert sent == [LIVE_PHRASE], f"боевая фраза не доехала до AddKeywords: {sent!r}"
    assert rep["keywords"] == 1
    assert not [e for e in (rep.get("errors") or []) if "отсея" in str(e)]

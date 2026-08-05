"""Измерения сверки UAC-копий (copy_service/copy_uac._copy_uac_verify_rows).

Формы полей зафиксированы по ЖИВОМУ ответу UAC ``GET /campaign/<id>`` (probe 2026-08-05,
porg-rgwzgo57 / 713318320). Скалярное чтение ``counters``/``goals`` давало ``unreadable``
на всех 26 кампаниях прогона a7c535bd9ba6 — тест держит именно реальные формы, чтобы
переименование/упрощение снова не выключило три измерения молча.
"""
from types import SimpleNamespace

from direct.copy_service.copy_uac import _copy_uac_verify_rows


def _spec(**over):
    base = dict(titles=["Заголовок"], texts=["Текст"], keywords=["купить авто"],
                counter_id=110881389, goal_id=586851002, region_ids=[11162],
                feed_id=None, minus_keywords=[])
    base.update(over)
    return SimpleNamespace(**base)


def _live(**over):
    # ровно та форма, что отдаёт UAC: counters — список, goals — список словарей
    base = {
        "counters": [110881389],
        "goals": [{"goal_id": 586851002, "goal_type": "OTHER", "cpa": 2000.0}],
        "feed_id": None, "listings_feed_id": None,
        "regions": [11162], "minus_keywords": [],
        "titles": ["Заголовок"], "texts": ["Текст"], "keywords": ["купить авто"],
    }
    base.update(over)
    return base


def _by_dim(rows):
    return {r["dimension"]: r["status"] for r in rows}


def test_real_uac_payload_reads_every_dimension():
    rows = _copy_uac_verify_rows({}, _live(), _spec(), source_id=1, target_id=2)
    statuses = _by_dim(rows)
    assert len(rows) == 9
    assert set(statuses.values()) == {"ok"}, statuses


def test_foreign_counter_and_goal_are_caught():
    rows = _copy_uac_verify_rows(
        {}, _live(counters=[999], goals=[{"goal_id": 777}]), _spec(), source_id=1, target_id=2)
    statuses = _by_dim(rows)
    assert statuses["uac_counter"] == "mismatch"
    assert statuses["uac_goal"] == "mismatch"


def test_master_campaign_without_feed_is_ok_not_unreadable():
    """У МК (tp6) фида нет вовсе: и spec, и live пустые — сверять нечего, но это не «не прочитано»."""
    statuses = _by_dim(_copy_uac_verify_rows({}, _live(), _spec(), source_id=1, target_id=2))
    assert statuses["uac_feed"] == "ok"


def test_product_campaign_feed_mismatch_is_caught():
    rows = _copy_uac_verify_rows(
        {}, _live(feed_id=111), _spec(feed_id=222), source_id=1, target_id=2)
    assert _by_dim(rows)["uac_feed"] == "mismatch"


def _minus_row(sent, stored):
    rows = _copy_uac_verify_rows(
        {}, _live(minus_keywords=stored), _spec(minus_keywords=sent), source_id=1, target_id=2)
    return next(r for r in rows if r["dimension"] == "uac_minus_keywords")


def test_direct_word_forms_are_not_a_loss():
    """Директ хранит минус-слова в своей форме и схлопывает совпавшие — это НЕ потеря.

    Формы сняты с живого кабинета porg-rgwzgo57 (2026-08-05): отправляли «екатеринбург»,
    «электроскутеры», «машины», «частный», «новый» — лежат «екатеринбурге»,
    «электроскутер», «машина», «частное», «нова»/«ново»; стоп-слова «в»/«на»/«от»
    сохраняются как «!в»/«!на»/«!от». Побуквенное сравнение давало 13 ложных «потерь»
    из 375, сравнение по общей основе спотыкалось на «новый»→«нова» (общее «нов» — три
    символа). Объём при этом сходится: 60→58, 31→30, 29→28.
    """
    sent = ["екатеринбург", "электроскутеры", "машины", "частный", "новый", "в", "на", "от"]
    stored = ["екатеринбурге", "электроскутер", "машина", "частное", "нова", "!в", "!на", "!от"]
    row = _minus_row(sent, stored)
    assert row["status"] == "ok", row["repair_hint"]
    assert (row["source"], row["target"]) == (8, 8)


def test_live_ratio_from_real_run_passes():
    """Реальные объёмы прогона 150becb3ae75: схлопывание форм даёт 95-97%, это норма."""
    for sent, stored in ((60, 58), (31, 30), (29, 28), (59, 58)):
        row = _minus_row([f"с{i}" for i in range(sent)], [f"ж{i}" for i in range(stored)])
        assert row["status"] == "ok", (sent, stored)


def test_real_transfer_failure_is_caught():
    """Настоящий отказ переноса — ноль или половина вместо всех — обязан быть виден."""
    assert _minus_row([f"с{i}" for i in range(40)], [])["status"] == "mismatch"
    assert _minus_row([f"с{i}" for i in range(40)], [f"ж{i}" for i in range(20)])["status"] == "mismatch"


def test_empty_minus_list_stays_ok():
    """Пустой список минус-слов у источника — валидный результат, не расхождение."""
    assert _minus_row([], [])["status"] == "ok"


def test_duplicates_in_spec_do_not_fake_a_mismatch():
    """Обе стороны нормализуются одинаково: дубль в spec — не расхождение.

    До правки spec считался сырым, а live схлопывался _copy_uac_strings, и любой повтор
    в исходном списке давал ложный mismatch.
    """
    rows = _copy_uac_verify_rows(
        {}, _live(minus_keywords=["бу", "аренда"]),
        _spec(minus_keywords=["бу", "аренда", "бу"]), source_id=1, target_id=2)
    assert _by_dim(rows)["uac_minus_keywords"] == "ok"

    rows_titles = _copy_uac_verify_rows(
        {}, _live(titles=["Заголовок"]), _spec(titles=["Заголовок", "Заголовок"]),
        source_id=1, target_id=2)
    assert _by_dim(rows_titles)["uac_titles"] == "ok"


def test_missing_field_degrades_to_unreadable_not_mismatch():
    """Fail-safe: нет поля в ответе — «не прочитано», а не ложное расхождение."""
    live = _live()
    live.pop("regions")
    assert _by_dim(_copy_uac_verify_rows({}, live, _spec(), source_id=1, target_id=2))["uac_regions"] == "unreadable"

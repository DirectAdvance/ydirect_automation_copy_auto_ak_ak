"""Веер tp5 по фидам обязан давать РАЗЛИЧИМЫЕ имена кампаний.

Живой инцидент 2026-07-28 (porg-pl6iavd5): в кабинете 12 кампаний tp5 всего с 2 уникальными
именами — 9 близнецов «Поиск + Динамика + Товарная галерея - Марки - КС + Автотаргетинг» и 3
близнеца «… - Haval - Марки - КС». План закладывал 6 позиций, у аккаунта 9 разрешённых фидов.

Причина: билдер идёт fan-out'ом по всем фидам аккаунта, а суффикс фида в имени подавлялся
условием `_keep_struct_name`, которое смотрело ТОЛЬКО на тип кампании (структурная — значит
имя как в слепке) и не учитывало, что фидов несколько. `_uniq` этого не ловит: метка фида
приклеивается уже ПОСЛЕ построения плана.

Правило (Семён 2026-07-28): один фид — имя как в слепке; несколько — метка фида обязательна.
"""

import re

import pytest

from direct.create import create_set_feed_builders as fb

SRC = None


def _keep_struct_name(feeds: list, *, single_feed=False, segment=None,
                      products_only=False, only_gks=None, only_cts=None) -> bool:
    """Ровно то выражение, что стоит в билдере (create_set_feed_builders.py ~1097)."""
    _multi_feed = len(feeds) > 1
    return (not _multi_feed) and bool(
        single_feed or segment or products_only or only_gks or only_cts)


ONE = [(1, "yandex", "https://site/yandex.xml")]
NINE = [(i, f"feed{i}", f"https://site/feed{i}.xml") for i in range(9)]


def test_single_feed_structural_keeps_slepok_name():
    assert _keep_struct_name(ONE, segment="Марки") is True


def test_many_feeds_structural_gets_feed_label():
    """Тот самый инцидент: 9 фидов + структурная кампания → имя обязано различаться."""
    assert _keep_struct_name(NINE, segment="Марки") is False


@pytest.mark.parametrize("kw", [
    {"single_feed": True}, {"products_only": True},
    {"only_gks": ["gk"]}, {"only_cts": ["ct0111"]},
])
def test_every_structural_flag_still_yields_label_on_many_feeds(kw):
    assert _keep_struct_name(NINE, **kw) is False
    assert _keep_struct_name(ONE, **kw) is True


def test_non_structural_always_gets_label():
    assert _keep_struct_name(ONE) is False
    assert _keep_struct_name(NINE) is False


def test_builder_source_guards_on_feed_count():
    """Выражение в бою действительно учитывает число фидов, а не только тип кампании."""
    import inspect
    src = inspect.getsource(fb._create_tp5_campaign)
    assert "_multi_feed = len(data[\"feeds\"]) > 1" in src
    assert re.search(r"_keep_struct_name\s*=\s*\(not _multi_feed\)", src)

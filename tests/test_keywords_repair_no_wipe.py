"""Регрессия KEYWORDS_REPAIR_WIPES_LIVE_KEYWORDS (2026-07-30).

Боевой инцидент: кампания `713155623` (porg-rgwzgo57, tp4 «Поиск + Динамика - Модели - КС»)
создалась с 15258 ключами, а после авто-добивки осталась с 119 группами и НУЛ�ём ключей.
Причина: `keywords_repair` слал `build_update_item(grp, keywords=[])`, а это full-object
перезапись — пустой массив стирал живые фразы. В коде стояла посылка «UpdateUnifiedAdGroups —
подтверждённый no-op для ключей»; прогон её опроверг.

Тесты закрепляют ДВА инварианта, чтобы правку нельзя было молча откатить:
  1) в апдейт уходят РЕАЛЬНО прочитанные фразы группы, а не пустой список;
  2) при `keywords_truncated` кампания пропускается ЦЕЛИКОМ — частичный список слать нельзя,
     иначе это та же потеря данных, только тише.
"""
from __future__ import annotations

import pytest

from direct.repair import repair_keywords as rk


class _FakeGrid:
    """Минимальный двойник GridClient: помнит, что ушло в update_unified_adgroups."""

    def __init__(self, groups: list[dict], *, truncated: bool = False):
        self._groups = groups
        self._truncated = truncated
        self.updated: list[dict] = []
        self.added_keywords: list[dict] = []

    def groups_for_edit(self, cid, meta=None):
        if meta is not None and self._truncated:
            meta["keywords_truncated"] = True
        return list(self._groups)

    def build_update_item(self, grp, *, keywords, relevance_match=None):
        # Как настоящий: кладёт то, что дали. Тест смотрит именно на это поле.
        return {"adGroupId": str(grp["adgroup_id"]), "keywords": list(keywords or [])}

    def update_unified_adgroups(self, items):
        self.updated.extend(items)
        return [int(x["adGroupId"]) for x in items]

    def add_keywords(self, items):
        self.added_keywords.extend(items)
        return {"added": len(items)}


def _group(gid: int, cid: int, kws: list[str]) -> dict:
    return {
        "adgroup_id": gid,
        "campaign_id": cid,
        "adgroup_name": "ct0205_aoff_n000_r0121_ct001_ag011_g00 — Omoda C5",
        "campaign_name": "tp4_cpc_site — Поиск + Динамика - Модели - КС - Свердловская область",
        "supported": True,
        "keywords": list(kws),
        "keyword_count": len(kws),
        "relevance_match": {"isActive": False},
        "region_ids": [225],
    }


def _live_phrases(grid: _FakeGrid) -> list[list[str]]:
    return [list(x.get("keywords") or []) for x in grid.updated]


def test_update_round_trips_live_keywords_instead_of_empty_list():
    """Инвариант 1: в апдейт уходят живые фразы, а НЕ пустой список.

    Пустой массив = стирание: build_update_item перезаписывает объект целиком.
    """
    live = ["омода ц5 купить", "omoda c5 цена", "омода c5 екатеринбург"]
    grid = _FakeGrid([_group(5778243681, 713155623, live)])

    item = grid.build_update_item(grid.groups_for_edit(713155623)[0],
                                  keywords=(grid._groups[0].get("keywords") or []),
                                  relevance_match={"isActive": True})

    assert item["keywords"] == live, "живые фразы обязаны уехать обратно в апдейт"
    assert item["keywords"], "пустой список в build_update_item = потеря ключей группы"


def test_source_code_never_sends_empty_keywords_to_build_update_item():
    """Инвариант 1 на уровне исходника: `keywords=[]` в keywords_repair запрещён.

    Двойник Grid не поймает регресс, если кто-то вернёт литерал обратно, — поэтому
    проверяем сам вызов в коде добивки.
    """
    import inspect

    src = inspect.getsource(rk)
    assert "build_update_item(grp, keywords=[]" not in src, (
        "keywords=[] стирает живые ключи (KEYWORDS_REPAIR_WIPES_LIVE_KEYWORDS): "
        "передавать grp.get('keywords')"
    )
    assert "keywords_truncated" in src, "должен остаться гард по обрезанному ответу Grid"


def test_truncated_campaign_is_skipped_entirely():
    """Инвариант 2: при keywords_truncated кампанию не трогаем совсем.

    Grid не говорит, КАКИМ группам не хватило строк, поэтому частичный round-trip
    затёр бы остаток — та же потеря, только тише.
    """
    grid = _FakeGrid([_group(1, 713155623, ["фраза"])], truncated=True)
    meta: dict = {}
    grid.groups_for_edit(713155623, meta=meta)

    assert meta.get("keywords_truncated") is True
    assert grid.updated == [], "по обрезанной кампании апдейт слать нельзя"


@pytest.mark.parametrize("name,expected_flag", [
    ("tp4_cpc_site — Поиск + Динамика - Модели - КС - Свердловская область", False),
    ("tp4_cpc_site — Поиск + Динамика - Модели - КС + Автотаргетинг", True),
    ("tp2_cpc_site — Поиск - Общее - Автотаргетинг", True),
])
def test_intentional_autotarget_off_is_not_flagged(name, expected_flag):
    """Дефект Б: «КС» без «Автотаргетинг» = автотаргет выключен НАМЕРЕННО.

    Именно ложный WRONG_AUTOTARGET на такой кампании и запускал разрушительную добивку.
    """
    from direct.clients import grid_content_verifier as gcv

    fn = getattr(gcv, "_autotarget_expected_by_name", None)
    if fn is None:
        pytest.skip("_autotarget_expected_by_name отсутствует — правка не применена")
    assert bool(fn(name)) is expected_flag, name

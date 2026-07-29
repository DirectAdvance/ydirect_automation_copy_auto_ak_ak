"""Именованные наборы минус-фраз слепка → библиотека минус-фраз аккаунта (v5).

Решение Семёна 2026-07-28: наборы из структуры слепка ({site_type}/_minus_sets/{slug}.json)
создаются в библиотеке аккаунта и привязываются к кампаниям tp2-tp5.

Лимиты Директа — офиц. справка, .claude/skills/yandex-direct/docs/keywords/negative-keywords-library.md:
  :12  до 30 наборов на аккаунт
  :22  4096 символов БЕЗ пробелов на набор
  :41  до трёх наборов на одну кампанию
docs/keywords/negative-keywords.md — не более 7 слов в минус-фразе.
"""

import json
import types

import pytest

from direct.create import create_set_minus as csm


def _mk_deps(pack_root, *, existing=None, add_log=None, get_log=None):
    """Стенд DI create_set_minus: фейковый kontent_pack + v5-клиент в памяти."""
    existing = existing if existing is not None else []
    next_id = {"v": 900001}

    def _v5_get(svc, token, login, fieldnames, criteria=None, extra=None):
        if get_log is not None:
            get_log.append(svc)
        assert svc == "negativekeywordsharedsets"
        return {"result": {"NegativeKeywordSharedSets": list(existing)}}

    def _v5_call(svc, method, token, login, params):
        if add_log is not None:
            add_log.append((svc, method, params))
        assert (svc, method) == ("negativekeywordsharedsets", "add")
        out = []
        for s in params["NegativeKeywordSharedSets"]:
            new_id = next_id["v"]
            next_id["v"] += 1
            existing.append({"Id": new_id, "Name": s["Name"],
                             "NegativeKeywords": list(s["NegativeKeywords"])})
            out.append({"Id": new_id})
        return {"result": {"AddResults": out}}

    return {
        "_SLEPOK_KEY": {"слепок_кудерко": "kuderko"},
        "_enabled_minus_words": lambda: ["отзывы"],
        "_v5_call": _v5_call,
        "_v5_err": lambda j: str((j or {}).get("error")),
        "_v5_get": _v5_get,
        "kp": types.SimpleNamespace(PACK_ROOT=str(pack_root)),
    }


def _write_sets(pack_root, site_type, slug, sets, *, bare_list=False):
    d = pack_root / site_type / "_minus_sets"
    d.mkdir(parents=True, exist_ok=True)
    payload = sets if bare_list else {"slug": slug, "site_type": site_type, "sets": sets}
    (d / f"{slug}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache():
    csm._NAMED_SETS_CACHE.clear()
    yield
    csm._NAMED_SETS_CACHE.clear()


# ── 1. Читатель структуры ────────────────────────────────────────────────────────────────────

def test_reader_reads_named_sets_and_resolves_slepok_alias(tmp_path):
    csm.configure(_mk_deps(tmp_path))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк"]},
                 {"name": "Конкуренты", "phrases": ["автосалон рога"]}])

    # и по каноничному ключу, и по имени слепка из БД/UI
    for slepok in ("kuderko", "Слепок_Кудерко"):
        sets = csm._read_slepok_minus_sets(slepok, "С пробегом")
        assert [s["name"] for s in sets] == ["банки", "Конкуренты"], slepok
        assert sets[0]["phrases"] == ["тинькофф", "сбербанк"]


def test_reader_accepts_bare_list_and_missing_file(tmp_path):
    csm.configure(_mk_deps(tmp_path))
    _write_sets(tmp_path, "Мультибренд", "kuderko",
                [{"name": "n", "phrases": ["a"]}], bare_list=True)
    assert csm._read_slepok_minus_sets("kuderko", "Мультибренд")[0]["name"] == "n"
    # файла нет → пусто, без исключения
    assert csm._read_slepok_minus_sets("kuderko", "Нет такого типа") == []


def test_reader_does_not_depend_on_minus_shared(tmp_path):
    """У kuderko нет ни {slug}_minus_shared.txt, ни ct-уровневого {slug}_minus.txt —
    именованные наборы должны читаться всё равно."""
    csm.configure(_mk_deps(tmp_path))
    _write_sets(tmp_path, "С пробегом", "kuderko", [{"name": "new общая", "phrases": ["бу"]}])
    assert not (tmp_path / "С пробегом" / "tp2").exists()
    assert csm._read_slepok_minus_sets("kuderko", "С пробегом")[0]["phrases"] == ["бу"]


# ── 2. Создание в библиотеке ─────────────────────────────────────────────────────────────────

def test_sets_created_in_library_with_same_name_and_phrase_count(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк", "втб"]},
                 {"name": "Конкуренты", "phrases": ["рога", "копыта"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом", city="Волгоград")

    assert res["ok"] is True and res["errors"] == []
    assert res["created"] == ["банки", "Конкуренты"] and res["reused"] == []
    assert len(res["ids"]) == 2
    sent = [p for (_svc, _m, p) in add_log]
    assert [p["NegativeKeywordSharedSets"][0]["Name"] for p in sent] == ["банки", "Конкуренты"]
    assert [len(p["NegativeKeywordSharedSets"][0]["NegativeKeywords"]) for p in sent] == [3, 2]


def test_second_run_reuses_by_name_and_creates_no_duplicates(tmp_path):
    add_log = []
    existing = []
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф"]},
                 {"name": "Конкуренты", "phrases": ["рога"]}])

    first = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")
    assert len(add_log) == 2 and len(existing) == 2

    second = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert len(add_log) == 2, "повторный прогон вызвал add — в библиотеке появятся дубли"
    assert len(existing) == 2
    assert second["created"] == [] and second["reused"] == ["банки", "Конкуренты"]
    assert second["ids"] == first["ids"]


def test_existing_foreign_named_set_is_reused_not_duplicated(tmp_path):
    """scherbakova/salamahin: набор «Минуса общие tp2» уже есть в кабинете — берём его id."""
    add_log = []
    existing = [{"Id": 555, "Name": "Минуса общие tp2"}]
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "Мультибренд", "scherbakova",
                [{"name": "минуса общие TP2", "phrases": ["отзывы"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-azsw6eyh", "scherbakova", "Мультибренд")

    assert res["ids"] == [555] and res["created"] == [] and add_log == []


def test_no_sets_in_structure_is_not_an_error(tmp_path):
    add_log, get_log = [], []
    csm.configure(_mk_deps(tmp_path, add_log=add_log, get_log=get_log))
    res = csm.ensure_named_minus_sets("tok", "porg-x", "terehov", "Мультибренд")
    assert res["ok"] is True and res["ids"] == [] and res["skipped"]
    assert get_log == [] and add_log == [], "без наборов не должно быть ни одного v5-запроса"


# ── 3. Лимиты — видимая ошибка, а не тихая обрезка ───────────────────────────────────────────

def test_set_over_char_budget_is_not_created_and_reports_error(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    big = [f"фразаномер{i:05d}" for i in range(400)]   # ~6000 симв. без пробелов
    assert sum(len(w) for w in big) > csm._MINUS_SHARED_SET_CHAR_BUDGET
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "огромный", "phrases": big},
                 {"name": "мелкий", "phrases": ["тинькофф"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["ok"] is False
    assert any("огромный" in e and "4096" in e for e in res["errors"]), res["errors"]
    assert res["created"] == ["мелкий"], "здоровый набор должен создаться несмотря на битый соседний"
    assert len(add_log) == 1, "набор сверх лимита не должен уходить в add обрезанным"


def test_account_set_limit_blocks_creation_with_visible_error(tmp_path):
    existing = [{"Id": 100 + i, "Name": f"чужой {i}"} for i in range(csm._MINUS_LIB_MAX_SETS_ACCOUNT)]
    add_log = []
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", [{"name": "банки", "phrases": ["тинькофф"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["ok"] is False and add_log == []
    assert any("30" in e for e in res["errors"]), res["errors"]


def test_phrase_longer_than_seven_words_is_dropped_and_reported(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    long_phrase = "один два три четыре пять шесть семь восемь"
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": [long_phrase, "тинькофф"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    sent = add_log[0][2]["NegativeKeywordSharedSets"][0]["NegativeKeywords"]
    assert sent == ["тинькофф"]
    assert any("7 слов" in e for e in res["errors"]), res["errors"]


# ── 4. Гео-гард: свой город не уезжает в минуса ──────────────────────────────────────────────

def test_own_city_phrases_are_stripped_from_named_set(tmp_path):
    add_log = []
    csm.configure(_mk_deps(tmp_path, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "Конкуренты", "phrases":
                    ["автосалон волгоград", "волгоградский дилер", "автосалон москва"]}])

    csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом",
                                city="Волгоград", region="Волгоградская область")

    sent = add_log[0][2]["NegativeKeywordSharedSets"][0]["NegativeKeywords"]
    assert sent == ["автосалон москва"], sent


# ── 5. Кэш + лок: один get+add на (login, slepok, site_type) ─────────────────────────────────

def test_cached_wrapper_queries_v5_once_per_account(tmp_path):
    get_log, add_log = [], []
    csm.configure(_mk_deps(tmp_path, get_log=get_log, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", [{"name": "банки", "phrases": ["тинькофф"]}])

    a = csm.ensure_named_minus_sets_cached("tok", "porg-x", "kuderko", "С пробегом")
    b = csm.ensure_named_minus_sets_cached("tok", "porg-x", "Слепок_Кудерко", "С пробегом")

    assert a is b
    assert len(get_log) == 1 and len(add_log) == 1


# ── 6. Режимы слепков ────────────────────────────────────────────────────────────────────────

def test_kuderko_minus_mode_is_explicit_group(tmp_path):
    """Групповые минуса kuderko (331/720 групп в кабинете) должны остаться: режим group."""
    assert csm._SLEPOK_MINUS_MODE["kuderko"] == "group"
    assert csm._SLEPOK_MINUS_MODE.get("kuderko", "group") == csm._SLEPOK_MINUS_MODE["kuderko"]
    # чужие режимы не поехали
    assert csm._SLEPOK_MINUS_MODE["scherbakova"] == "shared_set"
    assert csm._SLEPOK_MINUS_MODE["pavlov"] == "campaign"


# ── 7. Отбор кампаний tp2-tp5 и merge-привязка ───────────────────────────────────────────────

def test_only_tp2_tp5_campaigns_are_selected():
    from direct.create.create_set_apply_batches import select_campaign_ids_by_tp
    created = [
        {"id": 1, "name": "tp1_cpc_site_ct0146_aon_n000_r0002_ct001_ag011_g00 — Haval"},
        {"id": 2, "name": "tp2_cpc_site_ct0000_aon_n000_r0000_ct001_ag011_g00 — Марки"},
        {"id": 3, "name": "tp3_cpa_site_ct0010_aon_n000_r0000_ct010_ag001_g00 — Товарная"},
        {"id": 4, "name": "tp4_cpc_site_ct0283_aon_n000_r0000_ct001_ag011_g00 — Динамика"},
        {"id": 5, "name": "tp5_cpc_site_ct0009_aon_n000_r0000_ct009_ag011_g00 — Галерея"},
        {"id": 6, "name": "tp7_cpc_site_ct0111_aon_n000_r0000_ct010_ag001_g00 — Товарка"},
        {"id": 7, "name": "Системная кампания eLama"},
    ]
    assert select_campaign_ids_by_tp(created, (2, 3, 4, 5)) == [2, 3, 4, 5]


def test_attach_merges_and_keeps_existing_library_ids(monkeypatch):
    """Повторная привязка не затирает уже прикреплённые наборы и не плодит дублей в списке."""
    from direct.create import create_set_apply_batches as ab

    sent = {}

    class _FakeGrid:
        def _read_unified_campaign_update_payloads(self, ids):
            return {cid: {"id": str(cid), "libraryMinusKeywordsIds": ["555"]} for cid in ids}

        def _narrow_bases(self, payloads, ids, why):
            return dict(payloads), {}

        def _post(self, name, q, variables):
            sent["items"] = variables["input"]["campaignUpdateItems"]

            class _R:
                @staticmethod
                def json():
                    return {"data": {"updateCampaigns": {
                        "updatedCampaigns": [{"id": "701"}, {"id": "702"}],
                        "validationResult": {}}}}
            return _R()

    monkeypatch.setattr(ab, "_build_grid", lambda login, agency="": (None, _FakeGrid()))
    deps = type("D", (), {"account_ctx": staticmethod(lambda login: {"agency": "ag"})})()

    res = ab.apply_minus_sets_batch("porg-x", [701, 702], deps, minus_set_ids=[555, 900001])

    assert res["ok"] is True and res["applied"] == 2
    for it in sent["items"]:
        assert it["unifiedCampaign"]["libraryMinusKeywordsIds"] == ["555", "900001"]


# ── 8. Проводка в оркестраторе (контракт) ────────────────────────────────────────────────────

def test_orchestrator_wiring_is_present():
    """Гейт от «правку откатили молча»: dep экспортирован, блок зовёт обе части и только tp2-tp5."""
    import pathlib
    from direct import automation_runtime as ar

    assert "_ensure_named_minus_sets" in ar._create_set_orchestrator_deps()

    src = (pathlib.Path(ar.__file__).parent / "create" / "create_set_orchestrator.py").read_text(encoding="utf-8")
    assert "_select_ms_ids(_extract_created_ms(results), (2, 3, 4, 5))" in src, \
        "оркестратор должен отбирать ровно tp2-tp5 (tp1 — намеренно без минус-фраз)"
    assert "_apply_ms_batch(login, _ms_camp_ids, _repair_deps()" in src, \
        "привязка должна идти через существующий apply_minus_sets_batch"
    body = src.split("if callable(_ensure_named_minus_sets):")[1].split(
        "конец именованных наборов")[0]
    assert "_BATCH_MODE" not in body, \
        "блок наборов минус-фраз не должен попасть под гейт batch-аспектов (он off в проде)"

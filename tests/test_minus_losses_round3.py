"""Ревью-раунд 3 по коммиту f33aaef4 — две Important и три Minor.

  Imp1  «видимость потерь» доезжает до итогов джобы: потери прогона (аудитории не отправлены,
        минус-фразы не влезли, наборы не привязались) едут в result["losses"] и поднимают
        has_issues тем же гейтом, что и ошибки верификации. errors_log сам по себе джобу
        зелёной быть не мешал — он объявлен НЕзначимым и в API джоб не отдаётся.
  Imp2  имя склеенного набора минус-фраз не зависит от СОСТАВА наборов слепка →
        реюз по имени стабилен, сирот в библиотеке аккаунта (лимит 30) не появляется.
  Minor 3  нумерация «часть N/M» ушла вместе со старой схемой имён.
  Minor 4  сверка состава — один нормализатор на обе стороны; расхождение = только нехватка.
  Minor 5  повтор привязки наборов — только транспортный отказ, с backoff.
"""

import inspect
import json
import pathlib
import types

import pytest

from direct.create import create_job_status as cjs
from direct.create import create_set_minus as csm
from direct.create import create_set_response as csr


# ── Imp1: гейт has_issues видит потери ───────────────────────────────────────────────────────

def _loss(kind="minus_sets", count=1):
    return {"kind": kind, "count": count, "message": "потеря"}


def test_losses_alone_make_job_not_green():
    """Пустой ret_map / not_packed / частичный провал привязки — верификатор их не видит."""
    data = {"losses": [_loss("audiences_not_sent", 12)]}
    bd = cjs.compute_job_issues_breakdown("set", data)
    assert bd is not None, "потери обязаны поднимать has_issues"
    assert bd["losses"] == 1
    assert bd["live_errors"] == 0 and bd["lv_errors"] == 0 and bd["ver_errors"] == 0


def test_annotate_writes_has_issues_from_losses_only():
    data = {"losses": [_loss(), _loss("minus_sets_partial", 3)]}
    cjs.annotate_job_issues("set", data)
    assert data["has_issues"]["losses"] == 2
    # верификации не было — «нет данных» ≠ «ноль дефектов», флаг обязан остаться
    assert data["has_issues_unknown"] is True


def test_no_losses_and_clean_verification_stays_green():
    data = {"verification": {"summary": {"errors": 0}},
            "live_verification": {"summary": {"errors": 0, "warnings": 3}},
            "losses": []}
    cjs.annotate_job_issues("set", data)
    assert "has_issues" not in data and "has_issues_unknown" not in data


def test_existing_verification_counting_not_broken():
    """Старый путь: разбивка считается по lv/ver summary, ключи и суммы прежние."""
    data = {"verification": {"summary": {"errors": 1}},
            "live_verification": {"summary": {"errors": 2, "warnings": 5},
                                  "issues": [{"severity": "error", "campaign_id": 1},
                                             {"severity": "error", "campaign_id": 1},
                                             {"severity": "error", "campaign_id": 2},
                                             {"severity": "warning", "campaign_id": 3}]},
            "gate_skips": 4}
    cjs.annotate_job_issues("set", data)
    hi = data["has_issues"]
    assert hi["lv_errors"] == 2 and hi["ver_errors"] == 1 and hi["live_errors"] == 3
    assert hi["live_warnings"] == 5 and hi["gate_skips"] == 4
    assert hi["positions_with_errors"] == 2
    assert hi["losses"] == 0
    assert "has_issues_unknown" not in data, "summary есть → данные верификации есть"


def test_has_issues_unknown_still_marks_missing_verification():
    data = {"verification": {}, "live_verification": {}}   # деградированный постпроцесс
    cjs.annotate_job_issues("set", data)
    assert data["has_issues_unknown"] is True and "has_issues" not in data


def test_losses_gate_only_for_create_kinds():
    for kind in ("copy_campaigns", "delete_drafts", ""):
        data = {"losses": [_loss()]}
        cjs.annotate_job_issues(kind, data)
        assert "has_issues" not in data, kind


def test_terminal_statuses_that_reach_the_gate():
    """done / error / cancelled считаются; interrupted этот путь не проходит вовсе."""
    assert cjs.terminal_status_for_job("set", {"failed": 0})[0] == "done"
    assert cjs.terminal_status_for_job("set", {"failed": 2})[0] == "error"
    assert cjs.terminal_status_for_job("set", {"failed": 0}, cancelled=True)[0] == "cancelled"
    src = inspect.getsource(cjs.terminal_status_for_job)
    assert "interrupted" not in src, "interrupted ставит SQL-апдейт recover/watchdog, минуя result"


def test_response_payload_carries_losses():
    payload = csr.build_create_set_response(
        created=1, failed=0, launch=False, results=[], promo_note=None, callouts_note=None,
        units_block=False, units_switched=False, units_note=None, units_pending=0,
        deferred_id=None, deferred_at=None, losses=[_loss("not_packed", 40)])
    assert payload["losses"] == [_loss("not_packed", 40)]
    assert cjs.compute_job_issues_breakdown("set", payload)["losses"] == 1


def _orchestrator_src() -> str:
    """Исходник оркестратора по пути ФАЙЛА теста.

    Намеренно БЕЗ `import automation_runtime`: его импорт перенастраивает deps соседних
    модулей (configure на весь пакет) и протекает на другие файлы тестов в общем прогоне.
    """
    return (pathlib.Path(__file__).resolve().parent.parent
            / "create/create_set_orchestrator.py").read_text(encoding="utf-8")


def test_orchestrator_routes_known_losses_through_note_loss():
    src = _orchestrator_src()
    assert "def _note_loss(" in src
    assert "losses=_losses," in src, "потери обязаны доехать до result джобы"
    for kind in ("audiences_not_sent", "minus_sets", "minus_sets_not_attached",
                 "minus_sets_attach_failed", "minus_sets_partial", "minus_sets_exception"):
        assert f'_note_loss("{kind}"' in src, kind


# ── Minor 5: повтор привязки только на транспорте, с паузой ─────────────────────────────────

def test_attach_retry_only_on_transport_error_with_backoff():
    src = _orchestrator_src()
    block = src.split("_ms_att = _apply_ms_batch(", 1)[1]
    block = block[:block.index('_ms_report["attach"]')]
    assert 'elif _ms_att.get("error_kind") == "transport":' in block
    assert "time.sleep(_MS_RETRY_BACKOFF_SEC)" in block
    assert '_ms_report["retry"] = "none_validation_error"' in block, \
        "валидационный отказ повторять тем же payload бессмысленно"


# ── Imp2 / Minor 3: имя набора не зависит от состава ────────────────────────────────────────

def _sets(sizes):
    out = []
    for i, total in enumerate(sizes):
        n = total // 7
        out.append({"name": f"набор{i}", "phrases": [f"ф{i}{j:05d}" for j in range(n)]})
    return out


def _mk_deps(pack_root, *, existing=None, add_log=None):
    existing = existing if existing is not None else []
    next_id = {"v": 900001}

    def _v5_get(svc, token, login, fieldnames, criteria=None, extra=None):
        return {"result": {"NegativeKeywordSharedSets": list(existing)}}

    def _v5_call(svc, method, token, login, params):
        if add_log is not None:
            add_log.append((svc, method, params))
        out = []
        for s in params["NegativeKeywordSharedSets"]:
            new_id = next_id["v"]
            next_id["v"] += 1
            existing.append({"Id": new_id, "Name": s["Name"],
                             "NegativeKeywords": list(s["NegativeKeywords"])})
            out.append({"Id": new_id})
        return {"result": {"AddResults": out}}

    return {
        "_SLEPOK_KEY": {},
        "_enabled_minus_words": lambda: [],
        "_v5_call": _v5_call,
        "_v5_err": lambda j: str((j or {}).get("error")),
        "_v5_get": _v5_get,
        "kp": types.SimpleNamespace(PACK_ROOT=str(pack_root)),
    }


def _write_sets(pack_root, site_type, slug, sets):
    d = pack_root / site_type / "_minus_sets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(
        json.dumps({"slug": slug, "site_type": site_type, "sets": sets}, ensure_ascii=False),
        encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Кэш наборов + ВОССТАНОВЛЕНИЕ реальных deps модуля после стенда.

    `csm.configure` пишет в globals() модуля (kp, _SLEPOK_KEY, _v5_*), поэтому стенд без
    отката протекал на соседние файлы тестов в общем прогоне.
    """
    _saved = {k: csm.__dict__.get(k) for k in
              ("kp", "_SLEPOK_KEY", "_enabled_minus_words", "_v5_call", "_v5_get", "_v5_err")}
    _saved_deps = dict(csm._DEPS)
    csm._NAMED_SETS_CACHE.clear()
    yield
    csm._NAMED_SETS_CACHE.clear()
    csm._DEPS.clear()
    csm._DEPS.update(_saved_deps)
    for k, v in _saved.items():
        if v is None:
            csm.__dict__.pop(k, None)
        else:
            csm.__dict__[k] = v


def test_pack_name_is_fixed_by_position_not_by_content():
    a, _ = csm._pack_minus_sets(_sets([4020, 1586, 2415, 4045]), 4096, 3,
                                name_prefix="kuderko · С пробегом")
    b, _ = csm._pack_minus_sets(_sets([4090, 1600, 2415, 3000]), 4096, 3,
                                name_prefix="kuderko · С пробегом")
    c, _ = csm._pack_minus_sets(
        [{"name": "совсем другие имена", "phrases": ["альфа"]},
         {"name": "и границы", "phrases": ["бета"]},
         {"name": "третий", "phrases": ["гамма"]},
         {"name": "четвёртый", "phrases": ["дельта"]}],
        4096, 3, name_prefix="kuderko · С пробегом")
    assert [x["name"] for x in a] == [x["name"] for x in b]
    assert [x["name"] for x in c] == ["kuderko · С пробегом — минуса 1/3"]
    # «часть N/M» больше не участвует в именовании (Minor 3)
    assert not any("часть" in x["name"] for x in a + b + c)


def test_name_total_is_fixed_so_three_to_two_bins_does_not_rename():
    """M в имени — константа max_sets, иначе имя поехало бы при 3 корзинах → 2."""
    three, _ = csm._pack_minus_sets(_sets([4090, 4090, 4090]), 4096, 3, name_prefix="p")
    two, _ = csm._pack_minus_sets(_sets([4090, 4090]), 4096, 3, name_prefix="p")
    assert len(three) == 3 and len(two) == 2
    assert two == [] or [x["name"] for x in two] == [x["name"] for x in three][:2]


def test_editing_slepok_content_reuses_the_same_library_sets(tmp_path):
    """Главный сценарий Imp2: правка состава набора не должна плодить сирот в библиотеке."""
    add_log, existing = [], []
    csm.configure(_mk_deps(tmp_path, existing=existing, add_log=add_log))
    _write_sets(tmp_path, "С пробегом", "kuderko", _sets([4020, 1586, 2415, 4045]))
    first = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")
    assert len(add_log) == 3 and first["packed_into"] == 3

    # директолог дописал фраз → границы корзин уехали, состав другой
    csm._NAMED_SETS_CACHE.clear()
    _write_sets(tmp_path, "С пробегом", "kuderko", _sets([4090, 2000, 2415, 3500]))
    second = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert len(add_log) == 3, "новых наборов быть не должно — иначе старые остаются сиротами"
    assert second["created"] == []
    assert second["ids"] == first["ids"]
    assert len(existing) == 3, "в библиотеке аккаунта по-прежнему 3 набора"
    assert second.get("mismatch"), "расхождение состава при этом обязано быть ГРОМКИМ"


def test_generated_names_fit_conservative_length_cap(tmp_path):
    packed, _ = csm._pack_minus_sets(_sets([4090, 4090, 4090]), 4096, 3,
                                     name_prefix="о" * 300)
    assert all(len(p["name"]) <= csm._MINUS_SET_NAME_MAX for p in packed)


# ── Minor 4: сверка состава ────────────────────────────────────────────────────────────────

def test_superset_in_cabinet_is_not_a_mismatch(tmp_path):
    """Директолог дописал фраз руками — набор-надмножество это НЕ ошибка."""
    existing = [{"Id": 555, "Name": "банки",
                 "NegativeKeywords": ["тинькофф", "сбербанк", "своё добавленное"]}]
    csm.configure(_mk_deps(tmp_path, existing=existing))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res["reused"] == ["банки"]
    assert not res.get("mismatch"), res["errors"]
    assert res["errors"] == []


def test_double_space_phrase_is_not_reported_missing(tmp_path):
    existing = [{"Id": 555, "Name": "банки", "NegativeKeywords": ["альфа банк"]}]
    csm.configure(_mk_deps(tmp_path, existing=existing))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["альфа  банк"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert not res.get("mismatch"), res["errors"]
    assert res["errors"] == []


def test_real_missing_phrase_is_still_loud(tmp_path):
    existing = [{"Id": 555, "Name": "банки", "NegativeKeywords": ["тинькофф"]}]
    csm.configure(_mk_deps(tmp_path, existing=existing))
    _write_sets(tmp_path, "С пробегом", "kuderko",
                [{"name": "банки", "phrases": ["тинькофф", "сбербанк"]}])

    res = csm.ensure_named_minus_sets("tok", "porg-x", "kuderko", "С пробегом")

    assert res.get("mismatch") == ["банки"]
    assert any("сбербанк" in e for e in res["errors"]), res["errors"]


def test_norm_phrase_applies_to_both_sides():
    assert csm._norm_minus_phrase("  Альфа   Банк ") == csm._norm_minus_phrase("альфа банк")

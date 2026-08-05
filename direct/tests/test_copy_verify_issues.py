"""Гейт «готово с замечаниями» для copy-джоб (copy_service/copy_verify_issues.py)."""
from direct.copy_service.copy_verify_issues import (
    annotate_copy_job_issues,
    copy_verify_issues,
    copy_verify_measured,
)


def _result(rows, *, settled=None):
    """result джобы с in-job сверкой и (опционально) осевшей."""
    out = {"cookie_postprocess": {"copy_verify": {"results": rows, "summary": {}}}}
    if settled is not None:
        out["copy_verify_settled"] = {"results": settled, "summary": {}}
    return out


def test_clean_verify_gives_no_issues():
    result = _result([{"dimension": "keyword_count", "status": "ok"}])
    annotate_copy_job_issues(result)
    assert "has_issues" not in result
    assert "has_issues_unknown" not in result


def test_mismatch_rows_become_breakdown():
    result = _result([
        {"dimension": "callout_count", "status": "mismatch"},
        {"dimension": "callout_count", "status": "mismatch"},
        {"dimension": "promo_attached", "status": "missing"},
        {"dimension": "keyword_count", "status": "ok"},
    ])
    annotate_copy_job_issues(result)
    assert result["has_issues"]["mismatch"] == 2
    assert result["has_issues"]["missing"] == 1
    # самое частое измерение — первым, чтобы в UI попало в обрезанный список
    assert list(result["has_issues"]["dimensions"]) == ["callout_count", "promo_attached"]


def test_unreadable_alone_is_not_an_issue():
    """unreadable — tri-state «поле не пришло из Grid/v5», fail-safe, а не дефект копии."""
    result = _result([
        {"dimension": "utm_tracking", "status": "unreadable"},
        {"dimension": "keyword_count", "status": "ok"},
    ])
    annotate_copy_job_issues(result)
    assert "has_issues" not in result
    assert "has_issues_unknown" not in result   # измерения БЫЛИ, просто часть не прочиталась


def test_unreadable_counted_alongside_real_mismatch():
    result = _result([
        {"dimension": "utm_tracking", "status": "unreadable"},
        {"dimension": "callout_count", "status": "mismatch"},
    ])
    assert copy_verify_issues(result)["unreadable"] == 1


def test_uac_only_copy_is_unknown_not_clean():
    """Чистый UAC (tp6/tp7): измерений D1-D19 нет, только excluded_intentional.

    Ноль расхождений из нуля измерений — это «не сверяли», а не «сверено чисто»
    (живой пример: job 952b74e1865b, 3 кампании tp7, summary все нули).
    """
    result = _result([
        {"dimension": "geo_kw_source_residual", "status": "excluded_intentional"},
        {"dimension": "geo_neg_target_blocked", "status": "excluded_intentional"},
    ])
    assert copy_verify_measured(result) is False
    annotate_copy_job_issues(result)
    assert result["has_issues_unknown"] is True
    assert "has_issues" not in result


def test_uac_rows_make_pure_uac_copy_measured():
    """Чистый tp6/tp7: D1-D19 не строится, но измерения UAC делают прогон сверенным."""
    result = {"uac_verify": [
        {"scope": "uac:2", "dimension": "uac_titles", "status": "ok"},
        {"scope": "uac:2", "dimension": "uac_counter", "status": "ok"},
    ]}
    assert copy_verify_measured(result) is True
    annotate_copy_job_issues(result)
    assert "has_issues_unknown" not in result
    assert "has_issues" not in result


def test_uac_mismatch_surfaces_in_breakdown():
    result = {"uac_verify": [
        {"scope": "uac:2", "dimension": "uac_goal", "status": "mismatch"},
        {"scope": "uac:2", "dimension": "uac_feed", "status": "unreadable"},
    ]}
    annotate_copy_job_issues(result)
    assert result["has_issues"]["mismatch"] == 1
    assert result["has_issues"]["unreadable"] == 1
    assert list(result["has_issues"]["dimensions"]) == ["uac_goal"]


def test_uac_rows_add_to_v5_rows_not_replace():
    """Смешанный прогон (v5 + UAC): в разбивку попадают обе группы измерений."""
    result = _result([{"dimension": "callout_count", "status": "mismatch"}])
    result["uac_verify"] = [{"dimension": "uac_regions", "status": "mismatch"}]
    annotate_copy_job_issues(result)
    assert result["has_issues"]["mismatch"] == 2
    assert set(result["has_issues"]["dimensions"]) == {"callout_count", "uac_regions"}


def test_observation_dimension_does_not_raise_issues(monkeypatch):
    """Измерение на обкатке считается, но карточку не красит.

    Правило заведено ценой трёх живых прогонов: свежая сверка UAC трижды показывала
    расхождение, и все три раза виновата была она сама, а не копирование.
    """
    import direct.copy_service.copy_verify_issues as mod
    monkeypatch.setattr(mod, "_OBSERVATION_DIMENSIONS", frozenset({"uac_new_thing"}))
    result = _result([{"dimension": "uac_new_thing", "status": "mismatch"}])
    mod.annotate_copy_job_issues(result)
    assert "has_issues" not in result          # тревоги нет
    assert "has_issues_unknown" not in result  # но измерение было


def test_observation_dimension_is_visible_next_to_real_issues(monkeypatch):
    import direct.copy_service.copy_verify_issues as mod
    monkeypatch.setattr(mod, "_OBSERVATION_DIMENSIONS", frozenset({"uac_new_thing"}))
    result = _result([
        {"dimension": "callout_count", "status": "mismatch"},
        {"dimension": "uac_new_thing", "status": "mismatch"},
        {"dimension": "uac_new_thing", "status": "mismatch"},
    ])
    mod.annotate_copy_job_issues(result)
    assert result["has_issues"]["mismatch"] == 1
    assert list(result["has_issues"]["dimensions"]) == ["callout_count"]
    assert result["has_issues"]["observation_only"] == {"uac_new_thing": 2}


def test_no_observation_dimensions_by_default():
    """Пустой карантин — норма: все текущие измерения обкатаны на живых данных."""
    import direct.copy_service.copy_verify_issues as mod
    assert mod._OBSERVATION_DIMENSIONS == frozenset()


def test_missing_verify_block_is_unknown():
    result = {"source_login": "a", "target_login": "b"}
    annotate_copy_job_issues(result)
    assert result["has_issues_unknown"] is True


def test_settled_verify_wins_over_in_job():
    """Осевшая пере-сверка перезаписывает раннюю: она и есть правда о результате."""
    result = _result(
        [{"dimension": "sitelinks_present", "status": "mismatch"}],
        settled=[{"dimension": "sitelinks_present", "status": "ok"}],
    )
    annotate_copy_job_issues(result)
    assert "has_issues" not in result


def test_flags_are_recomputed_not_accumulated():
    """Повторный вызов после починки должен СНЯТЬ флаги, а не оставить старые."""
    result = _result([{"dimension": "callout_count", "status": "mismatch"}])
    annotate_copy_job_issues(result)
    assert result["has_issues"]
    result["copy_verify_settled"] = {"results": [{"dimension": "callout_count", "status": "ok"}]}
    annotate_copy_job_issues(result)
    assert "has_issues" not in result
    assert "has_issues_unknown" not in result

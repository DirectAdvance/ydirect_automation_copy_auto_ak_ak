"""Регрессия PACK_GKLESS_POSITION_READS_LEGACY_ONLY (2026-07-30).

Позиция структуры БЕЗ gk означает «весь ct», но `_read_keywords_exact` читала только легаси
`{slepok}.txt`. Если ключи пака разложены по per-group файлам `{slepok}__{slug}.txt`, а легаси
нет — читалось «ключей нет». На этом content-gap preflight встали три аккаунта (`porg-uy3huxcn`,
`porg-nqavjicg`, `porg-dmwfp3dk`), хотя контент лежал рядом: salamahin/ct0051 — 604 фразы в
`salamahin__chery_tiggo.txt`, terehov/ct0283 («С пробегом») — 57.

Инварианты:
  1) gk-less позиция видит объединение per-group файлов, когда легаси отсутствует;
  2) НЕПУСТОЙ легаси остаётся точным ответом — объединение его не подменяет;
  3) `_minus`-файлы не утекают в позитивы (иначе минус-фразы поедут в ключи).
"""
from __future__ import annotations

import pytest

from direct import kontent_pack as kp


@pytest.fixture()
def ct_keywords_dir(tmp_path, monkeypatch):
    """Каталог keywords одного ct + подмена _ct_dir на него."""
    kd = tmp_path / "Мультибренд" / "tp2" / "ct0051" / "keywords"
    kd.mkdir(parents=True)
    monkeypatch.setattr(kp, "_ct_dir", lambda segment, tp, ct: str(kd.parent))
    return kd


def _positive(**kw) -> list:
    return list((kp._read_keywords_exact("Мультибренд", "tp2", "ct0051", "salamahin", **kw)
                 or {}).get("positive") or [])


def test_gkless_position_unions_per_group_files_when_legacy_absent(ct_keywords_dir):
    """Инвариант 1: без легаси-файла gk-less позиция собирает все группы ct."""
    (ct_keywords_dir / "salamahin__chery_tiggo.txt").write_text("чери тигго купить\n", encoding="utf-8")
    (ct_keywords_dir / "salamahin__chery_tiggo_7.txt").write_text("чери тигго 7 цена\n", encoding="utf-8")

    got = _positive()

    assert "чери тигго купить" in got and "чери тигго 7 цена" in got, got
    assert len(got) == 2


def test_minus_files_never_leak_into_positives(ct_keywords_dir):
    """Инвариант 3: `…_minus.txt` идёт в минус-слова, а не в ключи."""
    (ct_keywords_dir / "salamahin__chery_tiggo.txt").write_text("чери тигго купить\n", encoding="utf-8")
    (ct_keywords_dir / "salamahin__chery_tiggo_minus.txt").write_text("бесплатно\n", encoding="utf-8")

    data = kp._read_keywords_exact("Мультибренд", "tp2", "ct0051", "salamahin")

    assert "бесплатно" not in (data.get("positive") or [])
    assert "бесплатно" in (data.get("minus") or [])


def test_nonempty_legacy_still_wins(ct_keywords_dir):
    """Инвариант 2: непустой легаси — точный ответ, объединение НЕ подмешивается.

    Иначе gk-less позиции с настроенным легаси-файлом молча получили бы чужие фразы.
    """
    (ct_keywords_dir / "salamahin.txt").write_text("легаси фраза\n", encoding="utf-8")
    (ct_keywords_dir / "salamahin__chery_tiggo.txt").write_text("групповая фраза\n", encoding="utf-8")

    got = _positive()

    assert got == ["легаси фраза"], got


def test_explicit_gk_reads_only_its_own_group(ct_keywords_dir):
    """Позиция С gk не затрагивается: читает свой файл, не объединение."""
    (ct_keywords_dir / "salamahin__chery_tiggo.txt").write_text("чери тигго купить\n", encoding="utf-8")
    (ct_keywords_dir / "salamahin__omoda_c5.txt").write_text("омода ц5 купить\n", encoding="utf-8")

    got = _positive(group="chery_tiggo")

    assert got == ["чери тигго купить"], got


def test_no_files_at_all_stays_empty(ct_keywords_dir):
    """Пустой пак остаётся пустым — фолбэк ничего не выдумывает."""
    assert _positive() == []

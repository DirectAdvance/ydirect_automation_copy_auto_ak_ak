"""Публикация слепка: контент и структура появляются вместе, и именно в этом порядке."""
from __future__ import annotations

import json

import pytest

from direct.slepki_code import slepki_publish as sp


@pytest.fixture()
def pack(tmp_path, monkeypatch):
    """Зеркало пака во временной папке; M3-запись подменена (ssh в тестах не ходим)."""
    root = tmp_path / "pack"
    monkeypatch.setattr(sp.se, "_dst_abs", lambda rel: str(root / rel))
    monkeypatch.setattr(sp.se, "_ssh_write_m3_map", lambda mapping: (True, ""))
    monkeypatch.setattr(sp.se, "_touch_pack_cache_marker", lambda: None)
    return root


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Структура слепков во временной папке — настоящий slepki_store, но не на боевых файлах."""
    d = tmp_path / "slepki"
    d.mkdir()
    monkeypatch.setattr(sp.slepki_store, "SLEPKI_DIR", d)
    monkeypatch.setattr(sp.slepki_store, "_ORDER_PATH", d / "_order.json")
    monkeypatch.setattr(sp.slepki_store, "_CACHE_SIG", None, raising=False)
    monkeypatch.setattr(sp.slepki_store, "_CACHE_VAL", None, raising=False)
    return d


def test_content_and_structure_land_together(pack, store):
    entry = {"key": "testslepok", "name": "Слепок_Тест", "site_types": []}
    files = {"Мультибренд/tp2/ct0031/keywords/testslepok.txt": "купить машину\n"}

    rep = sp.publish(entry, files)

    assert rep["ok"], rep
    assert (pack / "Мультибренд/tp2/ct0031/keywords/testslepok.txt").read_text() == "купить машину\n"
    assert json.loads((store / "testslepok.json").read_text())["key"] == "testslepok"
    assert "testslepok" in json.loads((store / "_order.json").read_text())


def test_structure_not_written_when_content_fails(pack, store, monkeypatch):
    """Главная гарантия: провал контента НЕ оставляет слепок видимым в UI без ключей."""
    monkeypatch.setattr(sp.se, "_ssh_write_m3_map", lambda mapping: (False, "m3 unreachable"))
    entry = {"key": "brokenslepok", "name": "Слепок_Битый", "site_types": []}

    rep = sp.publish(entry, {"Мультибренд/tp2/ct0031/keywords/brokenslepok.txt": "x\n"})

    assert not rep["ok"]
    assert not (store / "brokenslepok.json").exists()
    assert "структуру не пишу" in rep["error"]


def test_verify_catches_silent_truncation(pack, store, monkeypatch):
    """Dual-write отчитался «ок», а на диске другое — это находка, а не успех."""
    real = sp.se._dual_write_pack_files_map

    def lying_write(mapping):
        res = real(mapping)
        for rel in mapping:                                # портим файл после «успешной» записи
            (pack / rel).write_text("обрезано")
        return res

    monkeypatch.setattr(sp.se, "_dual_write_pack_files_map", lying_write)
    entry = {"key": "truncslepok", "name": "Слепок_Обрез", "site_types": []}

    rep = sp.publish(entry, {"Мультибренд/tp2/ct0031/keywords/truncslepok.txt": "полный текст\n"})

    assert not rep["ok"]
    assert rep["content"]["verify_failed"]
    assert not (store / "truncslepok.json").exists()


def test_content_only_publish_leaves_structure_alone(pack, store):
    rep = sp.publish(None, {"Мультибренд/tp2/ct0031/callouts/kuderko.txt": "гарантия\n"})

    assert rep["ok"]
    assert rep["structure"] is None
    assert list(store.iterdir()) == []


def test_summary_reports_numbers_not_adjectives(pack, store):
    rep = sp.publish({"key": "sumslepok", "name": "Слепок_Сум", "site_types": []},
                     {"Мультибренд/tp2/ct0031/keywords/sumslepok.txt": "a\n"})

    text = sp.summary(rep)

    assert "1/1 файлов" in text
    assert "переписано 1 part-файл(ов)" in text

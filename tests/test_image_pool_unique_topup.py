"""Добор картинок до 5 берёт только УНИКАЛЬНЫЕ креативы (правило Семёна 2026-07-28).

ФАКТ, на котором стоит правка (кампания 713096702, porg-pl6iavd5, группа ct0300 «Tenet»):
Manual/ct0300 физически содержит 4 креатива (Trade-in / Зимние шины / КАСКО / Топливная карта),
пятым добором каскад брал `_image_store/slepki/karavaev/porg-psm5h7q6/8ZN6fuwhY3sKUKPOJJHFzQ.png`
— ТОТ ЖЕ баннер «КАСКО в подарок», пересохранённый в другой файл:
  • путь и имя другие → прежний дедуп `dict.fromkeys` его не видел;
  • md5 другой (549664ef… ≠ f1937a7e…, 2021990 ≠ 2126596 байт) → байт-дедуп тоже не видел;
  • pHash СОВПАДАЕТ бит-в-бит (замер на LXC101, Pillow 12.2.0) → ловится только перцептивно.
Порог сравнения — ТОЧНОЕ равенство pHash: замер по всем 199 папкам `_manual/ct*` дал
минимальную дистанцию между РАЗНЫМИ легитимными креативами = 6, поэтому любой порог ≥6
схлопнул бы «Зимние шины» и «Топливную карту» в одну картинку.

Здесь pHash подменяется фейком (Pillow есть в проде `/root/venv`, но не в тестовой venv) —
проверяется ЛОГИКА пула, а не сам DCT.
"""
import direct.automation_runtime as ar
import direct.clients.uac_client as uac


# «Визуальные» идентификаторы: разные файлы с одним id = один и тот же креатив.
_VISUAL = {
    "manual_tradein.png": 1,
    "manual_shiny.png": 2,
    "manual_kasko.png": 3,
    "manual_fuel.png": 4,
    "karavaev_kasko_resaved.png": 3,     # тот же креатив, другой файл/имя/md5
    "karavaev_other.png": 5,
    "zubakin_a.png": 6,
    "zubakin_b.png": 7,
}


def _fake_phash(path, *a, **kw):
    import os
    return _VISUAL.get(os.path.basename(str(path)))


def _wire(monkeypatch, *, manual, own_slepok=(), any_slepok=(), explicit=()):
    """Каскад источников подменяется целиком; порядок ступеней не меняется."""
    monkeypatch.setattr(uac, "_image_phash", _fake_phash)
    monkeypatch.setattr(ar, "_slepok_is_auto", lambda _s: True)
    monkeypatch.setattr(ar, "_manual_creative_paths", lambda _ct: list(manual))
    monkeypatch.setattr(ar, "_filter_content_assets", lambda items, *a, **kw: list(items))
    monkeypatch.setattr(ar, "_prioritized_content_assets", lambda items, *a, **kw: list(items))
    monkeypatch.setattr(ar, "_explicit_content_assets_for", lambda *a, **kw: list(explicit))
    monkeypatch.setattr(ar.kp, "read_slepok_images", lambda *a, **kw: list(own_slepok))
    monkeypatch.setattr(ar.kp, "read_any_slepok_images", lambda *a, **kw: list(any_slepok))
    ar._IMG_POOL_WARNED.clear()                  # логи warn'ов — 1 строка на процесс, чистим


def _pool(**kw):
    return ar._creative_images_for_ct("Мультибренд", "tp1", "ct0300", "scherbakova", **kw)


def test_duplicate_from_other_slepok_not_used_as_fifth(monkeypatch):
    """Живой кейс: 4 уникальных в Manual + визуальный дубль КАСКО у чужого слепка →
    уходят 4 РАЗНЫХ, пятого-повтора нет."""
    _wire(monkeypatch,
          manual=["manual_tradein.png", "manual_shiny.png", "manual_kasko.png", "manual_fuel.png"],
          any_slepok=["karavaev_kasko_resaved.png"])
    got = _pool()
    assert got == ["manual_tradein.png", "manual_shiny.png", "manual_kasko.png", "manual_fuel.png"]
    assert len({_VISUAL[p] for p in got}) == len(got) == 4


def test_duplicate_does_not_stop_topup(monkeypatch):
    """Дубль не занимает слот и не обрывает каскад: за ним берётся следующий УНИКАЛЬНЫЙ."""
    _wire(monkeypatch,
          manual=["manual_tradein.png", "manual_shiny.png", "manual_kasko.png", "manual_fuel.png"],
          any_slepok=["karavaev_kasko_resaved.png", "karavaev_other.png"])
    got = _pool()
    assert len(got) == 5
    assert "karavaev_kasko_resaved.png" not in got
    assert got[-1] == "karavaev_other.png"
    assert len({_VISUAL[p] for p in got}) == 5


def test_six_unique_gives_five_distinct(monkeypatch):
    _wire(monkeypatch,
          manual=["manual_tradein.png", "manual_shiny.png", "manual_kasko.png", "manual_fuel.png"],
          any_slepok=["karavaev_other.png", "zubakin_a.png"])
    got = _pool()
    assert len(got) == 5
    assert len({_VISUAL[p] for p in got}) == 5


def test_same_creative_two_names_inside_one_source_counted_once(monkeypatch):
    """Один креатив под двумя именами В ОДНОМ источнике — тоже один раз."""
    _wire(monkeypatch,
          manual=["manual_kasko.png", "karavaev_kasko_resaved.png", "manual_shiny.png"])
    got = _pool()
    assert got == ["manual_kasko.png", "manual_shiny.png"]


def test_deficit_warns_and_does_not_block(monkeypatch, capsys):
    """Дефицит уникальных виден warning'ом (образец UAC_IMAGES_POOL_SHORT) и НЕ блокирует."""
    _wire(monkeypatch,
          manual=["manual_kasko.png", "karavaev_kasko_resaved.png", "manual_shiny.png"])
    got = _pool()
    out = capsys.readouterr().out
    assert len(got) == 2                          # кампания создаётся с тем, что есть
    assert "IMAGES_POOL_SHORT" in out
    assert "images-dedup" in out                  # дубль отброшен, а не потерян молча
    assert "уникальных картинок 2 при цели 5" in out


def test_full_pool_is_silent(monkeypatch, capsys):
    """Пул полный и без дублей → ни одной warn-строки (не зашумляем журнал воркера)."""
    _wire(monkeypatch,
          manual=["manual_tradein.png", "manual_shiny.png", "manual_kasko.png",
                  "manual_fuel.png", "zubakin_a.png"])
    got = _pool()
    out = capsys.readouterr().out
    assert len(got) == 5
    assert "IMAGES_POOL_SHORT" not in out and "images-dedup" not in out


def test_unreadable_file_without_phash_stays_unique(monkeypatch):
    """Нет Pillow/битый файл → pHash None, md5 не прочитан → путь считаем уникальным."""
    monkeypatch.setattr(uac, "_image_phash", lambda *a, **kw: None)
    got_a = ar._image_identity_key("/nope/does_not_exist_a.png")
    got_b = ar._image_identity_key("/nope/does_not_exist_b.png")
    assert got_a != got_b and got_a.startswith("x:")

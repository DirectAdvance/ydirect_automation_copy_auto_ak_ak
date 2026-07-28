"""Порядок источников ВИДЕО (правило Семёна 2026-07-28).

Ролики, залитые под конкретного директолога, живут в его папке слепка
(``_slepki_data/<слепок>/videos``), а не в общем пуле ``agency/Video/<ct>``. Порядок —
ДВА ЯРУСА, внутри каждого источники идут свой → общий → чужой:

  ЯРУС 1 (точная модель): 1) СВОЙ слепок → 2) ОБЩИЙ пул → 3) ЧУЖОЙ слепок;
  ЯРУС 2 (подмена бренда): 4) свой слепок → 5) общий пул → 6) чужой слепок.

Инцидент №1, который это чинит: ``Haval_Dargo_*`` Павлова лежали в общем пуле ``ct0112`` и
выигрывали у генеративных ``ct0112_*.mp4`` ЧИСТО ПО АЛФАВИТУ ('H' < 'c'), поэтому уезжали
в три чужие tp7-кампании (porg-pl6iavd5 / porg-azsw6eyh / porg-4ealp4ry).

Инцидент №2 (2026-07-28, «точный ролик Jolion важнее чужого Dargo»): brand-fallback общего
пула срабатывал ДО ступени «чужой слепок», поэтому для опустевших папок пула ct0118/ct0119/
ct0120 чужой слепок получал ``ct0112_*`` (Haval Dargo) вместо точного ролика своей модели
из пака Павлова.
"""
from direct import kontent_pack as kp

PACK = kp.M3_PACK_ROOT
VIDEO = kp.M3_VIDEO_ROOT


def _slepok_rel(folder: str, name: str) -> str:
    return f"{PACK}/_slepki_data/{folder}/videos/{name}"


def _pool_rel(ct: str, name: str) -> str:
    return f"{VIDEO}/{ct}/{name}"


def _patch(monkeypatch, *, pool: dict, slepki: dict, models: dict) -> None:
    """pool: {ct: [имена файлов]}; slepki: {папка: {ct|модель: [имена]}}; models: {ct: 'Brand Model'}."""
    index = {
        "external_assets": {
            f"Video|video|{ct}": [{"remote": _pool_rel(ct, n), "kind": "video_external"}
                                  for n in names]
            for ct, names in pool.items()
        },
        "slepki_data": {
            folder: {"videos": sorted({n for names in vmap.values() for n in names}),
                     "videos_map": vmap}
            for folder, vmap in slepki.items()
        },
    }
    monkeypatch.setattr(kp, "_load_index", lambda: index)
    monkeypatch.setattr(kp, "feeds_ct_model", lambda: dict(models))
    # Байты не трогаем: фетч = тождество, фильтр валидности пропускает всё.
    monkeypatch.setattr(kp, "_fetch_many", lambda rels: {r: r for r in rels})
    monkeypatch.setattr(kp, "_filter_valid_videos", lambda paths: list(paths))


_MODELS = {"ct0112": "Haval Dargo", "ct0299": "Haval Dargo X", "ct0119": "Haval Jolion",
           "ct0118": "Haval H9"}


def test_exact_model_in_foreign_slepok_beats_pool_brand_fallback(monkeypatch):
    """ЯРУС 1 > ЯРУС 2: точный Jolion из ЧУЖОГО слепка важнее Dargo из общего пула.

    Папка пула ``ct0119`` опустела (батч by_code уехал в пак Павлова) → brand-fallback пула
    подставлял ``ct0112_*`` (Haval Dargo). Теперь сначала ищется точная модель во всех
    источниках, и только потом подмена бренда."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},   # ct0119 в пуле пуст
        slepki={"pavlov": {"ct0119": ["Haval_Jolion_16x9.mp4", "Haval_Jolion_1x1.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-azsw6eyh", "ct0119", limit=2, brand_hint="Haval",
                           slepok="scherbakova")
    assert got == [_slepok_rel("pavlov", "Haval_Jolion_16x9.mp4"),
                   _slepok_rel("pavlov", "Haval_Jolion_1x1.mp4")]


def test_pool_brand_fallback_still_works_when_model_absent_everywhere(monkeypatch):
    """Точной модели нет НИГДЕ (ни в слепках, ни в пуле) → brand-fallback пула, как раньше."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},
        slepki={"pavlov": {"ct0112": ["Haval_Dargo_16x9.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-azsw6eyh", "ct0118", limit=2, brand_hint="Haval",
                           slepok="scherbakova")
    assert got == [_pool_rel("ct0112", "ct0112_01.mp4"), _pool_rel("ct0112", "ct0112_02.mp4")]


def test_own_slepok_wins_over_common_pool(monkeypatch):
    """Ступень 1: у слепка есть свой ролик на этот ct → берём его, общий пул не трогаем."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},
        slepki={"pavlov": {"ct0112": ["Haval_Dargo_16x9.mp4", "Haval_Dargo_1x1.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-pl6iavd5", "ct0112", limit=2, brand_hint="Haval", slepok="pavlov")
    assert got == [_slepok_rel("pavlov", "Haval_Dargo_16x9.mp4"),
                   _slepok_rel("pavlov", "Haval_Dargo_1x1.mp4")]


def test_no_own_video_takes_common_pool_not_foreign_slepok(monkeypatch):
    """Ступень 2: своего нет, общий пул НЕпуст → общий пул, а НЕ ролики чужого слепка."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},
        slepki={"pavlov": {"ct0112": ["Haval_Dargo_16x9.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-azsw6eyh", "ct0112", limit=2, brand_hint="Haval",
                           slepok="scherbakova")
    assert got == [_pool_rel("ct0112", "ct0112_01.mp4"), _pool_rel("ct0112", "ct0112_02.mp4")]


def test_foreign_slepok_is_last_fallback(monkeypatch):
    """Ступень 3: своего нет И общий пул пуст (в т.ч. brand-fallback) → ролик чужого слепка."""
    _patch(
        monkeypatch,
        pool={},                       # общего пула нет вовсе
        slepki={"pavlov": {"ct0299": ["Haval_Dargo_X_16x9.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-4ealp4ry", "ct0299", limit=2, brand_hint="Haval",
                           slepok="scherbakova")
    assert got == [_slepok_rel("pavlov", "Haval_Dargo_X_16x9.mp4")]


def test_pavlov_brand_ct_takes_own_videos_first(monkeypatch):
    """Павлов на брендовом ct0111 (Haval) получает СВОИ ролики, а не генеративные из пула."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},
        slepki={"pavlov": {"ct0112": ["Haval_Dargo_16x9.mp4"],
                           "ct0299": ["Haval_Dargo_X_16x9.mp4"]}},
        models=_MODELS,               # ct0111 в feeds нет — марка берётся из brand_hint
    )
    got = kp.videos_for_ct("porg-pl6iavd5", "ct0111", limit=2, brand_hint="Haval", slepok="pavlov")
    assert got == [_slepok_rel("pavlov", "Haval_Dargo_16x9.mp4"),
                   _slepok_rel("pavlov", "Haval_Dargo_X_16x9.mp4")]


def test_foreign_slepok_not_used_when_brand_ct_covered_by_pool(monkeypatch):
    """Чужой ролик Павлова НЕ уезжает в другой слепок, пока общий пул закрывает марку."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["ct0112_01.mp4", "ct0112_02.mp4"]},
        slepki={"pavlov": {"ct0112": ["Haval_Dargo_16x9.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-pl6iavd5", "ct0111", limit=2, brand_hint="Haval", slepok="kuderko")
    assert got == [_pool_rel("ct0112", "ct0112_01.mp4"), _pool_rel("ct0112", "ct0112_02.mp4")]


def test_pool_order_is_canonical_first_not_alphabetical(monkeypatch):
    """Регрессия инцидента: имя файла больше НЕ решает приоритет по алфавиту.

    'Haval_Dargo_16x9.mp4' обгонял 'ct0112_01.mp4' только из-за 'H' < 'c'. Теперь первыми
    идут канонические ролики пула ``ctNNNN[_NN].mp4``, затем всё остальное; внутри — натуральный
    порядок (ct0112_2 перед ct0112_10)."""
    _patch(
        monkeypatch,
        pool={"ct0112": ["Haval_Dargo_16x9.mp4", "ct0112_10.mp4", "ct0112_2.mp4"]},
        slepki={},
        models=_MODELS,
    )
    got = kp.videos_pool_for_ct("ct0112", limit=3)
    assert got == [_pool_rel("ct0112", "ct0112_2.mp4"),
                   _pool_rel("ct0112", "ct0112_10.mp4"),
                   _pool_rel("ct0112", "Haval_Dargo_16x9.mp4")]


def test_slepok_folder_by_login_suffix_still_works(monkeypatch):
    """Старая привязка «папка слепок-сборки заканчивается суффиксом логина» не сломана
    (haval_ufa_si7rw3ua ← porg-si7rw3ua), и модельный ключ карты по-прежнему читается."""
    _patch(
        monkeypatch,
        pool={"ct0119": ["ct0119_01.mp4"]},
        slepki={"haval_ufa_si7rw3ua": {"jolion": ["Jolion.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-si7rw3ua", "ct0119", limit=2)
    assert got == [_slepok_rel("haval_ufa_si7rw3ua", "Jolion.mp4")]


def test_ct_key_beats_degenerate_model_key(monkeypatch):
    """'Haval Dargo X' → модельный ключ вырождается в 'x'; ключ-ct в карте решает это."""
    assert kp._ct_model_key.__doc__          # функция существует (контракт для карт слепков)
    _patch(
        monkeypatch,
        pool={},
        slepki={"pavlov": {"ct0299": ["Haval_Dargo_X_9x16.mp4"]}},
        models=_MODELS,
    )
    got = kp.videos_for_ct("porg-pl6iavd5", "ct0299", limit=1, slepok="pavlov")
    assert got == [_slepok_rel("pavlov", "Haval_Dargo_X_9x16.mp4")]

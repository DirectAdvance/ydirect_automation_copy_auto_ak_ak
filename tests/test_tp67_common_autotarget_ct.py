"""Offline check: _tp67_common_autotarget_rec resolves brand ct, not ct0000, on Монобренд·Brand."""
from direct.create import create_set_context as csc


def _configure_stub(monkeypatch, ct_for_name_map=None):
    ct_for_name_map = ct_for_name_map or {}
    monkeypatch.setattr(csc, "_ct_for_name", lambda n: ct_for_name_map.get((n or "").strip().lower(), ""), raising=False)


def test_common_ct_stays_ct0000_for_multibrand(monkeypatch):
    _configure_stub(monkeypatch)
    ct = csc._tp67_common_ct_for_segment("Мультибренд", "site", [])
    assert ct == "ct0000"


def test_common_ct_stays_ct0000_for_monobrand_obshaya(monkeypatch):
    _configure_stub(monkeypatch)
    ct = csc._tp67_common_ct_for_segment("Монобренд · Общая", "site", [])
    assert ct == "ct0000"


def test_common_ct_resolves_majority_from_siblings(monkeypatch):
    _configure_stub(monkeypatch)
    merged = [
        {"sq": "site", "gc": "ct0111_aon_n000_r0000_ct010_ag001_g00"},
        {"sq": "site", "gc": "ct0111_aon_n000_r0000_ct010_ag001_g00"},
        {"sq": "kviz", "gc": "ct0999_aon_n000_r0000_ct010_ag001_g00"},
    ]
    ct = csc._tp67_common_ct_for_segment("Монобренд · Haval", "site", merged)
    assert ct == "ct0111"


def test_common_ct_falls_back_to_ct_for_name_when_no_siblings(monkeypatch):
    _configure_stub(monkeypatch, {"haval": "ct0111"})
    ct = csc._tp67_common_ct_for_segment("Монобренд · Haval", "site", [])
    assert ct == "ct0111"


def test_common_autotarget_rec_gc_uses_ct():
    rec = csc._tp67_common_autotarget_rec("tp7", "site", "ct0111")
    assert rec["gc"].startswith("ct0111_")
    rec6 = csc._tp67_common_autotarget_rec("tp6", "site", "ct0181")
    assert rec6["gc"].startswith("ct0181_")

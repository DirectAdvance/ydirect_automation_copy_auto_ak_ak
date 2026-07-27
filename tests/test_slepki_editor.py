from direct import slepki_editor


def test_tp2_keyword_preview_falls_back_to_tp1_group(tmp_path, monkeypatch):
    monkeypatch.setattr(slepki_editor.kp, "PACK_ROOT", str(tmp_path), raising=False)
    monkeypatch.setattr(slepki_editor.kp, "read_callouts", lambda *_args, **_kwargs: [], raising=False)

    kd = tmp_path / "Мультибренд" / "tp1" / "ct0179" / "keywords"
    kd.mkdir(parents=True)
    (kd / "scherbakova__knewstar_001.txt").write_text("купить knewstar из рся\n", encoding="utf-8")

    data = slepki_editor.read_group_keywords(
        "Мультибренд",
        "tp2",
        "ct0179",
        "scherbakova",
        group="knewstar_001",
    )

    assert data["kw_source"] == "tp1_fallback"
    assert data["positive"] == ["купить knewstar из рся"]


def test_tp67_keyword_preview_uses_group_aware_creation_reader(monkeypatch):
    calls = []

    monkeypatch.setattr(slepki_editor.kp, "_ct_dir", lambda *_args: "/missing", raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_group_slug", lambda value: value, raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_read_lines_opt", lambda *_args: ([], False), raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_read_lines", lambda *_args: [], raising=False)
    monkeypatch.setattr(slepki_editor.kp, "read_callouts", lambda *_args, **_kwargs: [], raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_dedup", lambda values: list(dict.fromkeys(values)), raising=False)

    def fake_tp67_keywords_for(*args, **kwargs):
        calls.append((args, kwargs))
        return ["fallback keyword"], ["fallback minus"]

    monkeypatch.setattr(slepki_editor, "_tp67_keywords_for", fake_tp67_keywords_for)

    data = slepki_editor.read_group_keywords(
        "Мультибренд",
        "tp6",
        "ct0000",
        "pavlov",
        group="mk_common",
        position="МК - Общая - КС + Автотаргетинг",
        target_label="КС + Автотаргетинг",
    )

    assert data["kw_source"] == "real_library"
    assert data["positive"] == ["fallback keyword"]
    assert calls
    assert calls[0][1]["group"] == "mk_common"


def test_tp67_pure_autotarget_preview_does_not_borrow_keywords(monkeypatch):
    calls = []

    monkeypatch.setattr(slepki_editor.kp, "_ct_dir", lambda *_args: "/missing", raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_group_slug", lambda value: value, raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_read_lines_opt", lambda *_args: ([], False), raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_read_lines", lambda *_args: ["borrowed keyword"], raising=False)
    monkeypatch.setattr(slepki_editor.kp, "read_callouts", lambda *_args, **_kwargs: [], raising=False)
    monkeypatch.setattr(slepki_editor.kp, "_dedup", lambda values: list(dict.fromkeys(values)), raising=False)

    def fake_tp67_keywords_for(*args, **kwargs):
        calls.append((args, kwargs))
        return ["fallback keyword"], []

    monkeypatch.setattr(slepki_editor, "_tp67_keywords_for", fake_tp67_keywords_for)

    data = slepki_editor.read_group_keywords(
        "Мультибренд",
        "tp7",
        "ct0000",
        "pavlov",
        group="tk_common_autotarget",
        position="ТК - Общее - Автотаргетинг",
        target_label="Автотаргетинг",
    )

    assert data["kw_source"] == "autotargeting"
    assert data["positive"] == []
    assert calls == []

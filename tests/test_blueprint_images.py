from direct.create import blueprint


def _patch_image_sources(monkeypatch, *, manual, slepok, explicit):
    monkeypatch.setattr(blueprint, "_slepok_is_auto", lambda value: True)
    monkeypatch.setattr(blueprint, "_image_ct_for_content", lambda value: "ct0031")
    monkeypatch.setattr(blueprint, "_manual_creative_paths", lambda value: list(manual))
    monkeypatch.setattr(blueprint, "_filter_content_assets", lambda paths, *args, **kwargs: list(paths))
    monkeypatch.setattr(blueprint.kp, "read_slepok_images", lambda *args, **kwargs: list(slepok))
    monkeypatch.setattr(
        blueprint, "_prioritized_content_assets", lambda paths, *args, **kwargs: list(paths)
    )
    monkeypatch.setattr(
        blueprint, "_explicit_content_assets_for", lambda *args, **kwargs: list(explicit)
    )


def test_non_common_ct_fills_manual_with_selected_slepok(monkeypatch):
    _patch_image_sources(
        monkeypatch,
        manual=["manual-1", "manual-2"],
        slepok=["slepok-1", "slepok-2", "slepok-3"],
        explicit=["explicit-1"],
    )

    result = blueprint._creative_images_for_ct("multi", "tp1", "ct0031", "kuderko", limit=5)

    assert result == ["manual-1", "manual-2", "slepok-1", "slepok-2", "slepok-3"]


def test_non_common_ct_uses_explicit_after_selected_slepok(monkeypatch):
    _patch_image_sources(
        monkeypatch,
        manual=["manual-1"],
        slepok=["slepok-1", "slepok-2"],
        explicit=["explicit-1", "explicit-2"],
    )

    result = blueprint._creative_images_for_ct("multi", "tp1", "ct0031", "kuderko", limit=5)

    assert result == ["manual-1", "slepok-1", "slepok-2", "explicit-1", "explicit-2"]

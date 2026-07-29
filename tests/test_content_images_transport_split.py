"""Разделение транспортов вкладки «Смена изображения» (tp6/tp7).

Инвариант: одна кампания — ОДИН транспорт, и ни одна кампания не остаётся
без транспорта вовсе. Регресс 2026-07-19: владение выводилось из tp-метки
ИМЕНИ кампании, а UAC ``list_campaigns`` — узкое подмножество tp6/tp7-по-имени
(архивные МК, tp7-товарка). Такие кампании Grid пропускал, а UAC не мог
обслужить → замена молча не выполнялась.
"""
from direct.content import content_images_routes as m
from direct.clients import grid_finalize as gf


# ───────────────────────────── _uac_owned_cids ──────────────────────────────

def test_owned_only_from_uac_contents():
    """Владение — только по факту чтения UAC-инвентарём."""
    assert m._uac_owned_cids({704589547: [{"id": "c1"}]}) == {704589547}


def test_tp6_by_name_outside_uac_is_not_owned():
    """РЕГРЕСС-КЕЙС: кампания 704589546 — tp6 по имени, но UAC её не читает.

    Grid обязан её обслуживать: иначе транспорта нет ни одного.
    """
    owned = m._uac_owned_cids({})
    assert owned == set()
    scan = m._grid_transport_scan({10: {"id": 10, "imageHashes": ["h1"]}},
                                  {10: 704589546}, "h1", owned)
    assert scan["cids"] == {704589546}


# ────────────────────────── _grid_transport_scan ────────────────────────────

def test_adaptive_in_uac_owned_is_blocked():
    """Адаптив в UAC-владеемой кампании — проекция contents, Grid его не пишет."""
    scan = m._grid_transport_scan({10: {"id": 10, "imageHashes": ["h1"]}},
                                  {10: 700}, "h1", {700})
    assert scan["cids"] == set()
    assert scan["blocked_uac"] == 1


def test_textad_in_uac_owned_stays_on_grid():
    """GdTextAd в UAC-владеемой кампании — НЕ проекция contents (живой замер
    porg-gcegsszl: grid_only=34 хэша в 6 кампаниях) → пишет его Grid-лег."""
    scan = m._grid_transport_scan(
        {10: {"id": 10, "kind": "text", "imageHashes": ["h1"]}},
        {10: 700}, "h1", {700})
    assert scan["cids"] == {700}
    assert scan["blocked_uac"] == 0


def test_rmw_unsafe_ad_gives_no_transport():
    scan = m._grid_transport_scan(
        {10: {"id": 10, "kind": "text", "imageHashes": ["h1"],
              "rmw_unsafe": "турболендинг"}},
        {10: 700}, "h1", set())
    assert scan["cids"] == set()
    assert scan["blocked_unsafe"] == 1


# ─────────────────────────── _annotate_transport ────────────────────────────

def _entry(key):
    return {"key": key, "supported": True, "reason": "",
            "usages": {"ads": 1, "campaigns": []}}


def test_annotate_marks_key_without_transport_unsupported():
    """Хэш только у адаптива UAC-владеемой кампании, в contents его нет → false."""
    merged = m._annotate_transport(
        [_entry("h1")], {10: {"id": 10, "imageHashes": ["h1"]}},
        {10: 700}, {}, {700})
    assert merged[0]["supported"] is False
    assert "contents" in merged[0]["reason"]


def test_annotate_keeps_textad_key_supported():
    """Тот же хэш на GdTextAd — транспорт есть, карточка остаётся выбираемой."""
    merged = m._annotate_transport(
        [_entry("h1")], {10: {"id": 10, "kind": "text", "imageHashes": ["h1"]}},
        {10: 700}, {}, {700})
    assert merged[0]["supported"] is True
    assert merged[0]["reason"] == ""


def test_annotate_keeps_uac_key_supported():
    """Хэш есть в contents → его заменит UAC-лег, Grid не нужен."""
    merged = m._annotate_transport(
        [_entry("h1")], {10: {"id": 10, "imageHashes": ["h1"]}},
        {10: 700}, {"h1": {}}, {700})
    assert merged[0]["supported"] is True


def test_annotate_reports_unsafe_reason():
    merged = m._annotate_transport(
        [_entry("h1")],
        {10: {"id": 10, "kind": "text", "imageHashes": ["h1"],
              "rmw_unsafe": "мультикарточки"}},
        {10: 700}, {}, set())
    assert merged[0]["supported"] is False
    assert "турболендинг" in merged[0]["reason"]


def test_uac_campaign_with_empty_contents_is_not_owned():
    """Пустые ``contents``: UAC заменять нечего — запрет Grid-лега оставил бы дыру."""
    assert m._uac_owned_cids({111: [], 222: [{"id": "c1"}]}) == {222}


def test_uac_inventory_failure_does_not_widen_ban():
    """Инвентарь UAC упал → запрет пуст, Grid-лег продолжает работать."""
    assert m._uac_owned_cids({}) == set()
    assert m._uac_owned_cids(None) == set()


# ───────────────────────────── архивные кампании ────────────────────────────

def test_rsya_inventory_excludes_archived_campaigns(monkeypatch):
    """Архивные кампании видны в Grid-list, но не попадают в write/index set."""
    seen = {}

    class _Grid:
        def _bootstrap_csrf(self):
            pass

        def adaptive_ads_for_update(self, cids, aids):
            return {}

        def text_ads_for_update(self, cids, aids):
            return {}

    monkeypatch.setattr(m, "_grid_campaigns", lambda grid, login: [
        {"id": "111", "name": "tp7_arch", "status": {"archived": True}},
        {"id": "222", "name": "tp7_live", "status": {"archived": False}},
    ])

    def _ads_index(grid, cids):
        seen["cids"] = list(cids)
        return []

    monkeypatch.setattr(m, "_grid_ads_index", _ads_index)
    from direct.web import routes_content_editor as rce
    monkeypatch.setattr(rce, "_grid_client", lambda login: _Grid())
    monkeypatch.setattr(rce, "_v5_paginate", lambda *a, **k: ([], None))

    _images, _ads, _ad_cid, skipped = m._rsya_inventory(
        "tok", "login", lambda *a, **k: {}, [])
    assert seen["cids"] == [222]
    assert skipped == [{"reason": "архивные кампании/объявления не изменяются", "count": 1}]


# ─────────────────────────── непересечение легов ────────────────────────────

def test_grid_skips_owned_and_reports_cids():
    ads_by_id = {1: {"id": 1, "imageHashes": ["old"]}, 2: {"id": 2, "imageHashes": ["old"]}}
    ad_cid = {1: 555, 2: 777}
    seen = {}

    class _Grid:
        def update_ad_images(self, items):
            seen["ids"] = [i["id"] for i in items]
            return len(items)

    out = m._replace_rsya_images("l", {"old": "new"}, ads_by_id, ad_cid,
                                 skip_cids={777}, grid_client_factory=lambda _l: _Grid())
    assert seen["ids"] == [1]                       # в UAC-владеемую 777 не писали
    assert out["ads_left_to_uac"] == 1
    assert out["left_to_uac_cids"] == [777]


def test_textad_in_owned_campaign_is_written_by_grid():
    """Текстовое объявление UAC-владеемой кампании пишет Grid (UpdateTextAds).

    До правки оно молча оставалось без транспорта: UAC-PATCH до GdTextAd не доходит
    (живой замер), а Grid-лег пропускал всю кампанию целиком.
    """
    ads_by_id = {1: {"id": 1, "kind": "text", "imageHashes": ["old"]},
                 2: {"id": 2, "imageHashes": ["old"]}}
    seen = {}

    class _Grid:
        def update_ad_images(self, items):
            seen["adaptive"] = [i["id"] for i in items]
            return len(items)

        def update_text_ad_images(self, items):
            seen["text"] = [i["id"] for i in items]
            return len(items)

    out = m._replace_rsya_images("l", {"old": "new"}, ads_by_id, {1: 777, 2: 777},
                                 skip_cids={777}, grid_client_factory=lambda _l: _Grid())
    assert seen["text"] == [1]                      # текстовое ушло Grid'ом
    assert "adaptive" not in seen                   # адаптивное — по-прежнему UAC-легу
    assert out["ads_left_to_uac"] == 1
    assert out["replaced"] == 1


# ─────────────────────── run_image_replace: сценарии ────────────────────────

class _FakeGrid:
    def __init__(self):
        self.uploads = 0
        self.written = None

    def upload_image(self, path):
        self.uploads += 1
        return "new_hash"

    def update_ad_images(self, items):
        self.written = [i["id"] for i in items]
        return len(items)

    def _bootstrap_csrf(self):
        pass

    def adaptive_ads_for_update(self, cids, aids):
        return {}


def _run(monkeypatch, tmp_path, *, rsya_images, ads_by_id, ad_cid,
         uac_images, uac_contents, uac_replaced_cids=()):
    """Прогон ``run_image_replace`` на фейках: сеть не трогается."""
    grid = _FakeGrid()
    monkeypatch.setattr(m, "_rsya_inventory",
                        lambda *a, **k: (rsya_images, ads_by_id, ad_cid, []))
    monkeypatch.setattr(m, "_uac_inventory", lambda *a, **k: (uac_images, uac_contents))
    monkeypatch.setattr(m, "_verify_uac_mirror",
                        lambda *a, **k: {"checked": 0, "ok": 0, "stale": [], "errors": []})
    monkeypatch.setattr(m, "_replace_uac_images",
                        lambda login, pairs, **k: {
                            "replaced": len(uac_replaced_cids), "errors": [],
                            "campaigns_touched": len(uac_replaced_cids),
                            "touched_ids": [str(c) for c in uac_replaced_cids]})

    class _Uac:
        def upload_image_file(self, path):
            return "c_new"

    from direct.web import routes_content_editor as rce
    monkeypatch.setattr(rce, "_uac_client", lambda login, factory=None: _Uac())

    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    res = m.run_image_replace(
        "tok", "porg-test",
        {"campaign_ids": [], "pairs": [{"old_key": "h_shared", "tmp_path": str(f)}]},
        lambda *a, **k: {}, grid_client_factory=lambda _l: grid)
    return res, grid


def test_tp6_outside_uac_goes_grid_path(monkeypatch, tmp_path):
    """704589546 (tp6 по имени, вне UAC) снова обрабатывается Grid-легом."""
    res, grid = _run(
        monkeypatch, tmp_path,
        rsya_images={"h_shared": {"key": "h_shared", "usages": {"ads": 1, "campaigns": [
            {"id": "704589546", "name": "tp6_cpc_site", "tp": "tp6", "ads": 1}]}}},
        ads_by_id={10: {"id": 10, "imageHashes": ["h_shared"]}},
        ad_cid={10: 704589546},
        uac_images={}, uac_contents={})
    assert grid.written == [10]
    assert res["replaced"] == 1
    assert res["errors"] == []


def test_mixed_case_unwritten_uac_campaign_is_loud(monkeypatch, tmp_path):
    """Хэш и в не-UAC кампании, и в UAC-владеемой, где UAC не отработал → ошибка."""
    res, grid = _run(
        monkeypatch, tmp_path,
        rsya_images={"h_shared": {"key": "h_shared", "usages": {"ads": 2, "campaigns": [
            {"id": "100", "name": "tp1", "tp": "tp1", "ads": 1},
            {"id": "200", "name": "tp6", "tp": "tp6", "ads": 1}]}}},
        ads_by_id={10: {"id": 10, "imageHashes": ["h_shared"]},
                   20: {"id": 20, "imageHashes": ["h_shared"]}},
        ad_cid={10: 100, 20: 200},
        uac_images={"h_other": {}},                 # h_shared в contents НЕ попал
        uac_contents={200: [{"id": "cX", "direct_image_hash": "h_other"}]},
        uac_replaced_cids=())
    assert grid.written == [10]                      # 200 отдана UAC-легу
    assert res["legs_reconcile"]["unwritten_cids"] == [200]
    assert any("UAC-лег в них не отработал" in e for e in res["errors"])


def test_no_double_write_when_uac_covers_campaign(monkeypatch, tmp_path):
    """UAC покрывает кампанию → Grid в неё не пишет, аплоад в Grid не делается."""
    res, grid = _run(
        monkeypatch, tmp_path,
        rsya_images={"h_shared": {"key": "h_shared", "usages": {"ads": 1, "campaigns": [
            {"id": "300", "name": "tp6", "tp": "tp6", "ads": 1}]}}},
        ads_by_id={30: {"id": 30, "imageHashes": ["h_shared"]}},
        ad_cid={30: 300},
        uac_images={"h_shared": {}},
        uac_contents={300: [{"id": "cA", "direct_image_hash": "h_shared"}]},
        uac_replaced_cids=(300,))
    assert grid.written is None                      # Grid-лега не было вовсе
    assert grid.uploads == 0                         # и двойного аплоада тоже
    assert res["errors"] == []
    assert res["replaced"] == 1


# ─────────── отказ Директа не должен выглядеть успехом (updatedAds:[null]) ───────────
#
# Живой probe 2026-07-19 (porg-gcegsszl, кампания 704132838): 15 items отклонены
# ``ACTION_IN_ARCHIVED_CAMPAIGN``, HTTP 200, ``updatedAds:[null ×15]`` — код брал
# ``len()`` и возвращал ``replaced:15, errors:[]`` при НУЛЕ изменённых объявлений.
# Сигнатура GRID_UPDATE_ADS_NULL_ITEMS_FALSE_SUCCESS.

_ARCHIVED = {"code": "BannerDefectIds.Gen.ACTION_IN_ARCHIVED_CAMPAIGN",
             "params": {"campaignId": "704132838"}, "path": "adUpdateItems[0]"}


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _grid_with_response(payload, gql_key):
    """GridClient без сети: ``__init__`` ходит за кукой, поэтому собираем вручную."""
    c = object.__new__(gf.GridClient)
    c.login = "porg-test"
    c.last_ad_update_errors = []
    c._bootstrap_csrf = lambda: None
    c._post = lambda *a, **k: _Resp({"data": {gql_key: payload}})
    return c


def _adaptive_items(n):
    return [{"id": str(i + 1), "imageHashes": ["h_new"], "titles": ["t"], "bodies": ["b"]}
            for i in range(n)]


def _text_items(n):
    return [{"id": str(i + 1), "imageHashes": ["h_new"], "title": "t", "body": "b"}
            for i in range(n)]


def test_adaptive_all_null_is_zero_and_loud():
    """Отказ по всем 2 объявлениям → replaced 0 + причина наверх (без фикса было 2)."""
    grid = _grid_with_response(
        {"updatedAds": [None, None], "validationResult": {"errors": [_ARCHIVED]}},
        "updateAdaptiveTextAds")
    assert grid.update_ad_images(_adaptive_items(2)) == 0
    assert grid.last_ad_update_errors
    assert "ACTION_IN_ARCHIVED_CAMPAIGN" in grid.last_ad_update_errors[0]
    assert "704132838" in grid.last_ad_update_errors[0]     # params прокинуты


def test_textad_all_null_is_zero_and_loud():
    grid = _grid_with_response(
        {"updatedAds": [None, None], "validationResult": {"errors": [_ARCHIVED]}},
        "updateAds")
    assert grid.update_text_ad_images(_text_items(2)) == 0
    assert "ACTION_IN_ARCHIVED_CAMPAIGN" in grid.last_ad_update_errors[0]


def test_mixed_null_counts_only_real_ids():
    """Смешанный ответ: одно записалось, второе — null. replaced=1 и это видно."""
    grid = _grid_with_response(
        {"updatedAds": [{"id": "1"}, None], "validationResult": {"errors": [_ARCHIVED]}},
        "updateAdaptiveTextAds")
    assert grid.update_ad_images(_adaptive_items(2)) == 1
    assert "обновлено 1 из 2" in grid.last_ad_update_errors[0]


def test_empty_updated_ads_is_loud_without_validation_errors():
    """Пустой список и ни одной причины — всё равно не успех."""
    grid = _grid_with_response({"updatedAds": [], "validationResult": None},
                               "updateAdaptiveTextAds")
    assert grid.update_ad_images(_adaptive_items(2)) == 0
    assert "без id" in grid.last_ad_update_errors[0]


def test_missing_updated_ads_key_is_loud():
    grid = _grid_with_response({}, "updateAds")
    assert grid.update_text_ad_images(_text_items(1)) == 0
    assert grid.last_ad_update_errors


def test_success_path_unchanged():
    """НЕ сломать успешный путь: реальные id + validationResult:null → replaced=N, тихо."""
    grid = _grid_with_response(
        {"updatedAds": [{"id": str(i + 1)} for i in range(15)], "validationResult": None},
        "updateAds")
    assert grid.update_text_ad_images(_text_items(15)) == 15
    assert grid.last_ad_update_errors == []

    grid_a = _grid_with_response(
        {"updatedAds": [{"id": "1"}], "validationResult": None}, "updateAdaptiveTextAds")
    assert grid_a.update_ad_images(_adaptive_items(1)) == 1
    assert grid_a.last_ad_update_errors == []


def test_textad_update_is_chunked_and_logs_failed_ids():
    """450-item job не должен падать в один Grid-батч; failed ids сохраняются."""
    c = object.__new__(gf.GridClient)
    c.login = "porg-test"
    c.last_ad_update_errors = []
    c._bootstrap_csrf = lambda: None
    calls = []

    def _post(_op, _q, variables):
        items = variables["updateInput"]["adUpdateItems"]
        calls.append([it["id"] for it in items])
        if len(calls) == 1:
            return _Resp({"data": {"updateAds": {
                "updatedAds": [{"id": it["id"]} for it in items],
                "validationResult": None,
            }}})
        return _Resp({"data": {"updateAds": {
            "updatedAds": [None for _ in items],
            "validationResult": {"errors": [_ARCHIVED]},
        }}})

    c._post = _post
    assert c.update_text_ad_images(_text_items(55)) == 50
    assert len(calls) == 2
    assert len(calls[0]) == 50
    assert calls[1] == ["51", "52", "53", "54", "55"]
    assert "failed_ad_ids=51,52,53,54,55" in c.last_ad_update_errors[0]


def test_warnings_alone_do_not_fail_full_success():
    """Только warnings при полном успехе — не ошибка задания."""
    grid = _grid_with_response(
        {"updatedAds": [{"id": "1"}],
         "validationResult": {"warnings": [{"code": "SomeWarn", "path": "x"}]}},
        "updateAdaptiveTextAds")
    assert grid.update_ad_images(_adaptive_items(1)) == 1
    assert grid.last_ad_update_errors == []


def test_replace_rsya_propagates_grid_reason_to_job_errors():
    """Сквозной инвариант: отказ Grid виден в результате задания, replaced=0."""
    class _Grid:
        def __init__(self):
            self.last_ad_update_errors = []

        def update_ad_images(self, items):
            self.last_ad_update_errors = ["UpdateAdaptiveTextAds: НЕ обновлено ни одного "
                                          "из 2 объявл. — ACTION_IN_ARCHIVED_CAMPAIGN"]
            return 0

    out = m._replace_rsya_images(
        "l", {"old": "new"},
        {1: {"id": 1, "imageHashes": ["old"]}, 2: {"id": 2, "imageHashes": ["old"]}},
        {1: 555, 2: 555}, grid_client_factory=lambda _l: _Grid())
    assert out["replaced"] == 0
    assert out["errors"]
    assert "ACTION_IN_ARCHIVED_CAMPAIGN" in out["errors"][0]


def test_replace_rsya_archived_ads_are_skipped_not_errors():
    """Архивные объявления внутри неархивной кампании не меняем и не считаем дефектом."""
    class _Grid:
        def __init__(self):
            self.last_ad_update_errors = []

        def update_text_ad_images(self, items):
            self.last_ad_update_errors = [
                "UpdateTextAds: НЕ обновлено ни одного из 2 объявл. — "
                "BannerDefectIds.Gen.CANNOT_UPDATE_ARCHIVED_AD @adUpdateItems[0]; "
                "BannerDefectIds.Gen.CANNOT_UPDATE_ARCHIVED_AD @adUpdateItems[1]; "
                "failed_ad_ids=1,2"
            ]
            return 0

    out = m._replace_rsya_images(
        "l", {"old": "new"},
        {1: {"id": 1, "kind": "text", "imageHashes": ["old"]},
         2: {"id": 2, "kind": "text", "imageHashes": ["old"]}},
        {1: 555, 2: 555}, grid_client_factory=lambda _l: _Grid())
    assert out["replaced"] == 0
    assert out["errors"] == []
    assert out["ads_archived"] == 2


def test_replace_rsya_mixed_archived_and_other_grid_error_is_loud():
    """Смешанный отказ нельзя прятать как штатный ad-level archive."""
    class _Grid:
        def __init__(self):
            self.last_ad_update_errors = []

        def update_text_ad_images(self, items):
            self.last_ad_update_errors = [
                "UpdateTextAds: НЕ обновлено ни одного из 2 объявл. — "
                "BannerDefectIds.Gen.CANNOT_UPDATE_ARCHIVED_AD @adUpdateItems[0]; "
                "DefectIds.Gen.SOME_OTHER_ERROR @adUpdateItems[1]; failed_ad_ids=1,2"
            ]
            return 0

    out = m._replace_rsya_images(
        "l", {"old": "new"},
        {1: {"id": 1, "kind": "text", "imageHashes": ["old"]},
         2: {"id": 2, "kind": "text", "imageHashes": ["old"]}},
        {1: 555, 2: 555}, grid_client_factory=lambda _l: _Grid())
    assert out["replaced"] == 0
    assert out["ads_archived"] == 0
    assert out["errors"]
    assert "SOME_OTHER_ERROR" in out["errors"][0]

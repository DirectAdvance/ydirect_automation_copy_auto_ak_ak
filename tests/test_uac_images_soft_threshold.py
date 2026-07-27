"""Мягкий порог картинок tp6/tp7 (решение Семёна 2026-07-27).

ФАКТ, на котором стоит правка: 5 — это ПОТОЛОК Яндекса, а не минимум.
`constants.validation.adConstants` в ответе `GET /wizard/web-api/aggregate` отдаёт
`maxNumberOfImages: 5` и НЕ содержит `minNumberOfImages` (HAR `direct.yandex.ru.5har.har`);
`POST /web-api/uac/campaigns` принят с `content_ids` длиной 1 (HAR 4har, кампания 710818592)
и длиной 3 вместе с `feed_id`+`listings_feed_id`, т.е. на ТОВАРНОЙ (HAR 5har, 710844579).

Проверяем ровно три вещи:
  А. пул своего ct меньше цели, взяли всё → НЕ ошибка и НИКАКОГО пересоздания;
  Б. пул полный, а в кампании меньше → ошибка + ровно ОДНА попытка пересоздания;
  В. второй попытки пересоздания не бывает НИ ПРИ КАКОМ размере пула.
"""
from pathlib import Path

from direct import repair_auto, repair_gate, repair_planner, uac_verifier


def _detail(images: int, *, videos: int = 0) -> dict:
    """Нормализованная live-строка UAC, где ВСЁ, кроме картинок, в порядке."""
    return {
        "status": "draft",
        "pricing": "PER_CLICK",
        "titles": 5,
        "texts": 3,
        "sitelinks": 8,
        "content": images + videos,
        "images": images,
        "videos": videos,
        "week_limit": 1000,
        "limit_period": "week",
        "regions": 1,
        "counters": 1,
        "goals": 1,
        "has_tracking_params": True,
        "has_feed": True,
        "has_model_filter": True,
    }


_NAME = "tp7_cpc_site_ct0111_aon_n000_r0002_ct010_ag001_g00 — ТК - Haval - КС + Автотаргетинг"


def test_case_a_pool_short_is_warning_not_error_and_never_recreates():
    """Пул ct0111 = 4 картинки, в кампании 4 → взяли всё, что есть: это НЕ ошибка."""
    issues, repair = uac_verifier.verify_uac_detail(
        _NAME, 123, _detail(4), {"images_pool": 4})

    codes = {i["code"] for i in issues}
    assert "UAC_IMAGES_LOW" not in codes
    assert "UAC_IMAGES_POOL_SHORT" in codes
    warn = next(i for i in issues if i["code"] == "UAC_IMAGES_POOL_SHORT")
    assert warn["severity"] == "warn"
    assert warn["actual"] == 4 and warn["pool"] == 4
    # никакого кандидата на пересоздание
    assert not [r for r in repair if r.get("kind") == "recreate_or_resume_campaign"]
    # и код физически не умеет породить recreate-действие
    assert "UAC_IMAGES_POOL_SHORT" not in repair_planner._RECREATE_CODES
    assert "UAC_IMAGES_POOL_SHORT" not in repair_gate._UAC_REPLACE_CODES
    plan = repair_planner.build_repair_plan({"issues": issues, "repair_candidates": repair})
    assert plan["actions"] == []
    assert plan["status"] == "empty"


def test_case_b_full_pool_but_fewer_images_in_campaign_is_error_with_repair():
    """Пул 12 картинок, а в кампании 4 → картинки теряются по дороге: настоящий дефект."""
    issues, repair = uac_verifier.verify_uac_detail(
        _NAME, 123, _detail(4), {"images_pool": 12})

    codes = {i["code"] for i in issues}
    assert "UAC_IMAGES_LOW" in codes
    assert "UAC_IMAGES_POOL_SHORT" not in codes
    err = next(i for i in issues if i["code"] == "UAC_IMAGES_LOW")
    assert err["severity"] == "error" and err["pool"] == 12
    assert any(r.get("kind") == "recreate_or_resume_campaign" for r in repair)


def test_partial_loss_inside_short_pool_is_still_error():
    """Пул 4, а в кампанию доехали 2 → потеря есть даже при коротком пуле."""
    issues, _ = uac_verifier.verify_uac_detail(
        _NAME, 123, _detail(2), {"images_pool": 4})
    assert "UAC_IMAGES_LOW" in {i["code"] for i in issues}


def test_unknown_pool_keeps_previous_strict_behaviour():
    """Пул не прокинут (старые джобы) → прежний error, чтобы не потерять реальную потерю."""
    issues, repair = uac_verifier.verify_uac_detail(_NAME, 123, _detail(4))
    assert "UAC_IMAGES_LOW" in {i["code"] for i in issues}
    assert any(r.get("kind") == "recreate_or_resume_campaign" for r in repair)


def test_zero_images_with_video_is_not_an_error_when_pool_is_empty():
    """Пул 0 картинок, но есть видео → показывать есть что: ошибки нет, только warn."""
    issues, repair = uac_verifier.verify_uac_detail(
        _NAME, 123, _detail(0, videos=2), {"images_pool": 0})
    codes = {i["code"] for i in issues}
    assert "UAC_IMAGES_LOW" not in codes
    assert "UAC_MEDIA_MISSING" not in codes          # видео = медиа есть
    assert "UAC_IMAGES_POOL_SHORT" in codes
    assert not [r for r in repair if r.get("kind") == "recreate_or_resume_campaign"]


def _snapshot(body: dict) -> dict:
    """Терминальная джоба с actionable-планом на пересоздание одной tp7."""
    plan = repair_planner.build_repair_plan({
        "issues": [{"severity": "error", "code": "UAC_IMAGES_LOW",
                    "name": _NAME, "id": 713081184, "actual": 4, "expected": 5}],
    })
    assert plan["status"] == "actionable"
    return {
        "login": "porg-pl6iavd5",
        "body": body,
        "result": {"login": "porg-pl6iavd5",
                   "results": [{"name": _NAME, "ok": True, "id": 713081184}],
                   "live_verification": {"repair_plan": plan}},
        "session": {},
    }


def test_recreate_happens_once_and_never_twice():
    """Ровно ОДНА попытка пересоздания: дочерняя (repair) джоба новой не порождает.

    Именно неограниченный повтор был живым дефектом (`porg-pl6iavd5`: delete → recreate →
    та же нехватка → delete → ...). Гейт — `_repair_parent_job_id` в теле дочерней джобы,
    который проставляет `repair_gate` при постановке ремонта.
    """
    parent_body = {"agency": "a", "items": [{"name": _NAME}]}
    first = repair_auto.auto_recreate_request("parent-1", _snapshot(parent_body))
    assert first and first.get("login") == "porg-pl6iavd5"     # первая попытка планируется

    # тело дочерней джобы строит сам repair_gate (не подделываем флаг руками)
    child_body = repair_gate.repair_queue_body(
        parent_body,
        [{"name": _NAME}],
        parent_job_id="parent-1",
    )
    assert child_body.get("_repair_parent_job_id") == "parent-1"
    # вторая попытка НЕ планируется, даже если после пересоздания картинок снова меньше 5
    assert repair_auto.auto_recreate_request("child-1", _snapshot(child_body)) is None


def test_create_path_no_longer_blocks_on_short_pool():
    """Позиция больше не отбивается «Кампания не создана» из-за короткого пула."""
    src = Path(__file__).resolve().parents[1].joinpath("create_set_master_product.py").read_text(
        encoding="utf-8")
    assert "CONTENT_GAP_IMAGES_LOW" not in src          # блокировка по «пул < 5» удалена
    assert '"images_pool": _pool_size' in src           # пул уезжает в результат для верификатора
    assert "if _pool_size < _create_min and not it_videos:" in src   # блок только без креативов


def test_thresholds_are_env_tunable_without_code_change(monkeypatch):
    monkeypatch.setenv("DIRECT_UAC_IMAGES_CREATE_MIN", "0")
    assert uac_verifier.images_create_min() == 0        # блокировку можно снять совсем
    monkeypatch.setenv("DIRECT_UAC_IMAGES_CREATE_MIN", "5")
    assert uac_verifier.images_create_min() == 5        # и вернуть прежнюю жёсткость
    monkeypatch.delenv("DIRECT_UAC_IMAGES_CREATE_MIN")
    assert uac_verifier.images_create_min() == 1
    assert uac_verifier._env_int("DIRECT_UAC_IMAGES_MIN", 5) == 5

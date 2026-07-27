"""Pure UAC/tp6-tp7 invariant checks for post-create live verification."""
from __future__ import annotations

import os
import re
from typing import Any


_TP_PREFIX_RE = re.compile(r"^tp(?P<tp>\d+)_(?P<pay>cpc|cpa)_(?P<surface>site|kviz)", re.I)
_CPC_PRICING = {"PER_CLICK"}
_CPA_PRICING = {"PER_CONVERSION", "PER_ACTION"}


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


# ЦЕЛЕВОЕ число картинок UAC (используется в verifier и в create_set_master_product).
# Один источник правды — не дублируем хардкод в разных файлах.
# ⚠️ 5 — это ПОТОЛОК Яндекса, а НЕ его минимум (факт, 2026-07-27): собственные validation-константы
# Директа `constants.validation.adConstants` отдают `maxNumberOfImages: 5` и НЕ содержат ключа
# `minNumberOfImages` (HAR `direct.yandex.ru.5har.har`, `GET /wizard/web-api/aggregate`), а живое
# создание принято API и с меньшим числом креативов: `POST /web-api/uac/campaigns` → HTTP 201 при
# `content_ids` длиной 1 (HAR 4har, кампания 710818592) и длиной 3 вместе с `feed_id`+
# `listings_feed_id`, т.е. ТОВАРНАЯ tp7 (HAR 5har, кампания 710844579).
# Поэтому порог = цель качества, а не условие создания. Меняется без правки кода:
# env `DIRECT_UAC_IMAGES_MIN`.
UAC_IMAGES_MIN = _env_int("DIRECT_UAC_IMAGES_MIN", 5)


def images_create_min() -> int:
    """Жёсткий нижний порог для СОЗДАНИЯ tp6/tp7 (env ``DIRECT_UAC_IMAGES_CREATE_MIN``, дефолт 1).

    Решение Семёна 2026-07-27: «мне нужна кампания даже с 4 изображениями, это лучше чем вообще
    её не будет». Дефицит пула создание больше НЕ блокирует; блокируем только вырожденный случай
    «показывать нечего вообще» (ноль картинок И ноль видео). `=0` полностью снимает блокировку,
    `=5` возвращает прежнее жёсткое поведение — без правки кода.
    """
    return _env_int("DIRECT_UAC_IMAGES_CREATE_MIN", 1)

# callable(ct_code:str) -> 'Общее'|'Марки'|'Модели'|None — инъекция из blueprint (see configure).
_SEGMENT_OF = None


def configure(deps: dict) -> None:
    """Инъекция зависимостей. Ждём `_ct_segment` — резолвер ct-кода в сегмент слепка,
    чтобы модельный фильтр требовать ТОЛЬКО для сегмента «Модели» (см. _tp7_requires_model_filter)."""
    global _SEGMENT_OF
    _SEGMENT_OF = deps.get("_ct_segment") or _SEGMENT_OF


def _campaign_pay_mode(name: str) -> str:
    m = _TP_PREFIX_RE.search(name or "")
    return (m.group("pay").lower() if m else "")


def _tp7_requires_model_filter(name: str) -> bool:
    """Модельный фильтр (model/folder_id) обязателен для товарки ТОЛЬКО сегмента «Модели» — она
    должна крутиться по конкретной модели, а не по всему фиду. Сегменты «Общее» (Автокредит/кредит,
    Прочие/Общие запросы, Автосалон, Дилер, Интересы) и «Марки» идут по всему фиду → фильтр НЕ нужен.
    Раньше требовался для любого ct≠{0000,0111} → ложный UAC_PRODUCT_MODEL_FILTER_MISSING на ct-«Общее»
    (ct0001/ct0006 = «Общее»). Резолвер сегмента инъектится через configure(); без него — прежний фолбэк."""
    if not re.search(r"^tp7_", name or "", re.I):
        return False
    m = re.search(r"_ct(\d{4})(?:_|\b)", name or "", re.I)
    if not m:
        return False
    ct = m.group(1)
    if ct in {"0000", "0111"}:
        return False
    if _SEGMENT_OF is not None:
        try:
            return str(_SEGMENT_OF(f"ct{ct}") or "").strip() == "Модели"
        except Exception:  # noqa: BLE001
            pass
    return True


def _repair(name: str, cid: int | None) -> dict[str, Any]:
    return {"kind": "recreate_or_resume_campaign", "name": name, "id": cid}


def _verify_struct_vs_live(nm: str, cid: int | None, detail: dict[str, Any],
                           struct: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Сверка «СТРУКТУРА СЛЕПКА → КАБИНЕТ» для tp6/tp7 (Д7, 2026-07-19).

    Зачем отдельно от build↔live: та сверка сравнивает отчёт билдера с кабинетом, а при обрыве
    на этапе ПЛАНА билдер уже пустой → ``0 == 0``, расхождения нет, отчёт зелёный, а ключи и
    аудитории потеряны (job ``9b2e040edf67``: структура 416 ключей / 9 аудиторий → кабинет 0/0).
    Эталон здесь берётся ПРЯМО из структуры слепка (``create_set_context.tp67_struct_expectations``),
    поэтому решение движка «по дороге» проверку обмануть не может.

    tri-state: live-счётчик не прочитан (``None``) → молчим; эталон 0 → сверять нечего.
    """
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    for label, key, code in (("ключи", "keywords", "UAC_STRUCT_KEYWORDS_MISSING"),
                             ("аудитории", "audiences", "UAC_STRUCT_AUDIENCES_MISSING")):
        try:
            expected = int(struct.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if expected <= 0:
            continue
        live = detail.get(key)
        if not isinstance(live, int):
            continue                     # поле не прочитано → tri-state fail-safe
        if live <= 0:
            issues.append({"severity": "error", "code": code, "name": nm, "id": cid,
                           "dimension": label, "expected": expected, "actual": live,
                           "note": (f"{label}: структура слепка даёт {expected}, "
                                    f"в кабинете 0 (потеря на этапе плана/сборки)")})
            repair.append(_repair(nm, cid))
        elif live < expected:
            issues.append({"severity": "warn", "code": code + "_UNDERCOUNT", "name": nm, "id": cid,
                           "dimension": label, "expected": expected, "actual": live,
                           "note": (f"{label}: структура слепка даёт {expected}, "
                                    f"в кабинете {live} (часть могла схлопнуться Директом)")})
    return issues, repair


def verify_uac_detail(name: str, campaign_id: int | None,
                      detail: dict[str, Any],
                      expected: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(issues, repair_candidates)`` for one normalized UAC detail row.

    ``expected["struct"]`` — эталон СТРУКТУРЫ слепка для позиции
    (``{"keywords": int, "audiences": int, ...}``, кладётся создателем кампании в result-строку).
    Отсутствует → сверка «структура → кабинет» просто не выполняется (обратная совместимость).
    """
    nm = str(name or "")
    cid = campaign_id
    issues: list[dict[str, Any]] = []
    repair: list[dict[str, Any]] = []
    tp = 7 if re.search(r"^tp7_", nm, re.I) else 6
    title_n = int(detail.get("titles") or 0)
    text_n = int(detail.get("texts") or 0)
    sitelink_n = int(detail.get("sitelinks") or 0)
    image_n = int(detail.get("images") or 0)
    media_n = int(detail.get("content") or 0) or int(detail.get("images") or 0) + int(detail.get("videos") or 0)
    status = str(detail.get("status") or "").lower()
    pay_mode = _campaign_pay_mode(nm)
    pricing = str(detail.get("pricing") or "").upper()

    if status and status not in {"draft", "drafted"}:
        issues.append({"severity": "error", "code": "UAC_NOT_DRAFT", "name": nm, "id": cid, "actual": status})
        repair.append({"kind": "stop_or_recreate_campaign", "name": nm, "id": cid})
    if pay_mode == "cpc" and pricing and pricing not in _CPC_PRICING:
        issues.append({"severity": "error", "code": "UAC_PRICING_MISMATCH",
                       "name": nm, "id": cid, "actual": pricing, "expected": "PER_CLICK"})
        repair.append(_repair(nm, cid))
    if pay_mode == "cpa" and pricing and pricing not in _CPA_PRICING:
        issues.append({"severity": "error", "code": "UAC_PRICING_MISMATCH",
                       "name": nm, "id": cid, "actual": pricing, "expected": sorted(_CPA_PRICING)})
        repair.append(_repair(nm, cid))
    if detail.get("week_limit") is not None and float(detail.get("week_limit") or 0) <= 0:
        issues.append({"severity": "error", "code": "UAC_BUDGET_MISSING",
                       "name": nm, "id": cid, "actual": detail.get("week_limit")})
        repair.append(_repair(nm, cid))
    limit_period = str(detail.get("limit_period") or "").lower()
    if limit_period and limit_period != "week":
        issues.append({"severity": "error", "code": "UAC_LIMIT_PERIOD_MISMATCH",
                       "name": nm, "id": cid, "actual": limit_period, "expected": "week"})
        repair.append(_repair(nm, cid))
    if int(detail.get("counters") or 0) <= 0:
        issues.append({"severity": "error", "code": "UAC_COUNTER_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if int(detail.get("goals") or 0) <= 0:
        issues.append({"severity": "error", "code": "UAC_GOAL_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if int(detail.get("regions") or 0) <= 0:
        issues.append({"severity": "error", "code": "UAC_REGION_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if not detail.get("has_tracking_params"):
        issues.append({"severity": "warn", "code": "UAC_UTM_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if detail.get("yandex_maps_enabled") is True:
        issues.append({"severity": "error", "code": "UAC_MAPS_ENABLED", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if detail.get("alternative_texts_enabled") is True:
        issues.append({"severity": "error", "code": "UAC_ALTERNATIVE_TEXTS_ENABLED", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if detail.get("recommendations_management_enabled") is True:
        issues.append({"severity": "error", "code": "UAC_RECOMMENDATIONS_ENABLED", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if detail.get("price_recommendations_management_enabled") is True:
        issues.append({"severity": "error", "code": "UAC_PRICE_RECOMMENDATIONS_ENABLED", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if title_n < 5:
        issues.append({"severity": "error", "code": "UAC_TITLES_MISSING",
                       "name": nm, "id": cid, "actual": title_n, "expected": 5})
        repair.append(_repair(nm, cid))
    if text_n < 3:
        issues.append({"severity": "error", "code": "UAC_TEXTS_MISSING",
                       "name": nm, "id": cid, "actual": text_n, "expected": 3})
        repair.append(_repair(nm, cid))
    if sitelink_n < 8:
        issues.append({"severity": "warn", "code": "UAC_SITELINKS_MISSING",
                       "name": nm, "id": cid, "actual": sitelink_n, "expected": 8})
        repair.append(_repair(nm, cid))
    if media_n <= 0:
        issues.append({"severity": "warn", "code": "UAC_MEDIA_MISSING", "name": nm, "id": cid, "actual": media_n})
        repair.append(_repair(nm, cid))
    # Картинки: РАЗВЕДЕНЫ два разных случая (решение Семёна 2026-07-27).
    #  А. В пуле СВОЕГО ct физически меньше UAC_IMAGES_MIN → взяли всё, что есть → это НЕ ошибка:
    #     warn `UAC_IMAGES_POOL_SHORT`, которого НЕТ ни в `repair_planner._RECREATE_CODES`, ни в
    #     `repair_gate._UAC_REPLACE_CODES` → пересоздания не планируется вовсе. Добор из чужого
    #     `ct`/`ct0000` запрещён (DOD §230), поэтому «добить до 5» тут физически нечем.
    #  Б. Пул ≥ UAC_IMAGES_MIN, а в кампанию попало меньше → картинки теряются по дороге, это
    #     настоящий дефект: error `UAC_IMAGES_LOW` + repair-кандидат (ровно ОДНА попытка
    #     пересоздания — второй не будет, гарантия в `repair_auto.auto_recreate_request`:
    #     дочерняя джоба несёт `_repair_parent_job_id` и авто-recreate по ней не планируется).
    # `expected["images_pool"]` кладёт создатель кампании (`create_set_master_product._res`).
    # Пул неизвестен (старые джобы / вызов не из потока создания) → прежнее поведение (error),
    # а страховкой от цикла остаётся пересчёт пула в `create_set_repairing._queue_recreate_repair_job`.
    _pool_raw = (expected or {}).get("images_pool")
    _pool_n = (int(_pool_raw) if isinstance(_pool_raw, int) and not isinstance(_pool_raw, bool)
               and _pool_raw >= 0 else None)
    _eff_min = UAC_IMAGES_MIN if _pool_n is None else min(UAC_IMAGES_MIN, _pool_n)
    if image_n < _eff_min:
        issues.append({"severity": "error", "code": "UAC_IMAGES_LOW",
                       "name": nm, "id": cid, "actual": image_n, "expected": _eff_min,
                       "pool": _pool_n, "target": UAC_IMAGES_MIN,
                       "note": (f"в пуле своего ct доступно {_pool_n} картинок, "
                                f"а в кампании {image_n} — картинки теряются по дороге"
                                if _pool_n is not None else
                                f"нужно {UAC_IMAGES_MIN} изображений именно своего ct; "
                                "видео не засчитывается как картинка")})
        repair.append(_repair(nm, cid))
    elif image_n < UAC_IMAGES_MIN:
        issues.append({"severity": "warn", "code": "UAC_IMAGES_POOL_SHORT",
                       "name": nm, "id": cid, "actual": image_n, "expected": UAC_IMAGES_MIN,
                       "pool": _pool_n,
                       "note": (f"в пуле своего ct всего {_pool_n} картинок — взяли все; "
                                f"добор из чужого ct/ct0000 запрещён. Чтобы стало {UAC_IMAGES_MIN}, "
                                "добавить картинки в Manual/<ct>/. Кампания оставлена как есть, "
                                "пересоздание не планируется")})
    if tp == 7 and not detail.get("has_feed"):
        issues.append({"severity": "error", "code": "UAC_FEED_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if tp == 7 and _tp7_requires_model_filter(nm) and not detail.get("has_model_filter"):
        issues.append({"severity": "error", "code": "UAC_PRODUCT_MODEL_FILTER_MISSING",
                       "name": nm, "id": cid, "fields": detail.get("feed_filter_fields") or []})
        repair.append(_repair(nm, cid))
    # TP7_CATALOG_FILTER_EMPTY: collectionId-фильтр задан, но values пусты → 0 страниц каталога.
    # Отличается от UAC_PRODUCT_MODEL_FILTER_MISSING (тот проверяет НАЛИЧИЕ model-фильтра).
    # Tri-state: catalog_filter_has_values=None (нет collection-фильтра) → молчим; False → error.
    if (tp == 7 and detail.get("has_collection_filter")
            and detail.get("catalog_filter_has_values") is False):
        issues.append({"severity": "error", "code": "TP7_CATALOG_FILTER_EMPTY",
                       "name": nm, "id": cid,
                       "fields": detail.get("feed_filter_fields") or [],
                       "note": "коллекционный фильтр фида задан, но values пусты — 0 страниц каталога"})
        repair.append(_repair(nm, cid))
    _struct = (expected or {}).get("struct")
    if isinstance(_struct, dict) and _struct:
        _si, _sr = _verify_struct_vs_live(nm, cid, detail, _struct)
        issues.extend(_si)
        repair.extend(_sr)
    return issues, repair

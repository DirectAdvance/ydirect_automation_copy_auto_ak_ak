"""Pure UAC/tp6-tp7 invariant checks for post-create live verification."""
from __future__ import annotations

import re
from typing import Any


_TP_PREFIX_RE = re.compile(r"^tp(?P<tp>\d+)_(?P<pay>cpc|cpa)_(?P<surface>site|kviz)", re.I)
_CPC_PRICING = {"PER_CLICK"}
_CPA_PRICING = {"PER_CONVERSION", "PER_ACTION"}

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
    if image_n < 5:
        issues.append({"severity": "error", "code": "UAC_IMAGES_LOW",
                       "name": nm, "id": cid, "actual": image_n, "expected": 5,
                       "note": "нужно 5 изображений именно своего ct; видео не засчитывается как картинка"})
        repair.append(_repair(nm, cid))
    if tp == 7 and not detail.get("has_feed"):
        issues.append({"severity": "error", "code": "UAC_FEED_MISSING", "name": nm, "id": cid})
        repair.append(_repair(nm, cid))
    if tp == 7 and _tp7_requires_model_filter(nm) and not detail.get("has_model_filter"):
        issues.append({"severity": "error", "code": "UAC_PRODUCT_MODEL_FILTER_MISSING",
                       "name": nm, "id": cid, "fields": detail.get("feed_filter_fields") or []})
        repair.append(_repair(nm, cid))
    _struct = (expected or {}).get("struct")
    if isinstance(_struct, dict) and _struct:
        _si, _sr = _verify_struct_vs_live(nm, cid, detail, _struct)
        issues.extend(_si)
        repair.extend(_sr)
    return issues, repair

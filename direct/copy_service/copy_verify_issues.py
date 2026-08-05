"""Гейт «готово с замечаниями» для copy-джоб: разбивка расхождений в result джобы.

Зачем отдельно от ``_copy_verify_blockers`` (copy_postprocess): тот решает, ронять ли джобу
в ``error``, и смотрит ТОЛЬКО ``_COPY_HARD_VERIFY_DIMENSIONS``. Всё, что не хард и не
объявлено report-only, проваливалось между двумя условиями и не попадало никуда: джоба
показывала «✅ готово» при 47 расхождениях (`41f128d3ed87`, 2026-08-04: callout_count
разошёлся на всех 22 кампаниях, promo_attached на 10, site_monitoring на 8).

Второй молчаливый случай — сверки не было ВОВСЕ. У чистого UAC/tp6/tp7-копирования
snapshot v5 пустой, измерений D1–D19 не строится ни одного, и summary приходит нулевым.
Ноль расхождений из нуля измерений читается как «сверено чисто» — это враньё, поэтому
такой исход помечается отдельным флагом, а не нулями в разбивке.

Формат намеренно тот же, что у create-очереди (`create/create_job_status.annotate_job_issues`):
``has_issues`` = разбивка, ``has_issues_unknown`` = сверки не было. Статус джобы НЕ меняем —
у copy партийные счётчики уже показаны в UI (см. `terminal_status_for_job`, там это решение
зафиксировано для copy/delete явно).
"""
from __future__ import annotations

from typing import Any


# Строки сверки, которые не считаются расхождением: ok и намеренно исключённые измерения
# (ad_price/bid_modifiers/geo_* — у copy свой стандарт поверх источника, см. COPY_INDEX.md).
_NON_ISSUE_STATUSES = ("ok", "excluded_intentional")

# ── Карантин для НОВЫХ измерений ──────────────────────────────────────────────
# Измерение отсюда считается и пишется в результат, но НЕ поднимает has_issues.
#
# Правило заведено ценой трёх живых прогонов (2026-08-05, porg-2wkbqwqe → porg-rgwzgo57):
# свежая сверка UAC трижды показала расхождение, и все три раза виновата была она сама,
# а не копирование. Побуквенное сравнение не знало, что Директ хранит минус-слова в своих
# формах; сравнение по общей основе спотыкалось на «новый»→«нова»; и только третья версия,
# по объёму, сошлась. Каждая итерация стоила прогона по живому кабинету и оставила там
# кампании-дубли.
#
# Поэтому новое измерение сначала живёт ЗДЕСЬ: его видно в result и можно сверить с живыми
# данными, но оно не красит карточку и не учит людей игнорировать предупреждения. Убирать
# отсюда — после того, как измерение подтверждено на реальном прогоне.
#
# Пустое множество — нормальное состояние: все текущие измерения обкатаны.
_OBSERVATION_DIMENSIONS: frozenset = frozenset()


def _verify_rows(result: dict[str, Any] | None) -> list[dict] | None:
    """Строки последней сверки: осевшая важнее in-job (она перезаписывает первую).

    К ним добавляются измерения UAC (``uac_verify``): у чистого tp6/tp7-копирования
    v5-снапшот пуст и D1-D19 не строится вовсе, а проверка при создании кампании есть —
    без этих строк прогон выглядел бы как «сверка не выполнялась».

    None = сверки нет ни в каком виде (не то же самое, что пустой список измерений).
    """
    if not isinstance(result, dict):
        return None
    rows: list[dict] = []
    for block in (result.get("copy_verify_settled"),
                  (result.get("cookie_postprocess") or {}).get("copy_verify")
                  if isinstance(result.get("cookie_postprocess"), dict) else None):
        if isinstance(block, dict) and isinstance(block.get("results"), list):
            rows = [r for r in block["results"] if isinstance(r, dict)]
            break
    uac_rows = result.get("uac_verify")
    if isinstance(uac_rows, list):
        rows = rows + [r for r in uac_rows if isinstance(r, dict)]
    return rows or None


def copy_verify_measured(result: dict[str, Any] | None) -> bool:
    """True, если сверка реально измерила хоть одно измерение.

    Пустой список строк и список из одних ``excluded_intentional`` — это «не мерили»,
    а не «померили и всё сошлось».
    """
    rows = _verify_rows(result)
    if not rows:
        return False
    return any(str(r.get("status") or "") != "excluded_intentional" for r in rows)


def copy_verify_issues(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Разбивка расхождений сверки или None, если всё сошлось / мерить было нечего.

    ``unreadable`` в разбивку входит справочно и САМ ПО СЕБЕ её не поднимает: это
    tri-state «поле не пришло из Grid/v5», fail-safe, а не дефект копии.
    """
    rows = _verify_rows(result)
    if not rows:
        return None
    mismatch = missing = unreadable = 0
    dimensions: dict[str, int] = {}
    observed: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        if status in _NON_ISSUE_STATUSES:
            continue
        dim = str(row.get("dimension") or "?")
        # Измерение на обкатке: считаем и показываем отдельно, но карточку не красим —
        # непроверенная мерка не должна выглядеть как дефект копирования.
        if dim in _OBSERVATION_DIMENSIONS:
            observed[dim] = observed.get(dim, 0) + 1
            continue
        if status == "unreadable":
            unreadable += 1
            continue
        if status == "missing":
            missing += 1
        else:
            mismatch += 1
        dimensions[dim] = dimensions.get(dim, 0) + 1
    if not (mismatch or missing):
        return None
    breakdown = {
        "mismatch": mismatch,
        "missing": missing,
        "unreadable": unreadable,
        # какие именно измерения разошлись и сколько раз — чтобы в UI было видно «что», а не только «сколько»
        "dimensions": dict(sorted(dimensions.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
    if observed:
        breakdown["observation_only"] = dict(sorted(observed.items()))
    return breakdown


def annotate_copy_job_issues(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Проставить в result джобы ``has_issues`` / ``has_issues_unknown``. Мутирует на месте.

    Оба флага независимы и переставляются заново при каждом вызове: осевшая пере-сверка
    может как закрыть расхождения первой сверки, так и впервые их показать.
    """
    if not isinstance(result, dict):
        return result
    issues = copy_verify_issues(result)
    if issues:
        result["has_issues"] = issues
    else:
        result.pop("has_issues", None)
    if copy_verify_measured(result):
        result.pop("has_issues_unknown", None)
    else:
        result["has_issues_unknown"] = True
    return result

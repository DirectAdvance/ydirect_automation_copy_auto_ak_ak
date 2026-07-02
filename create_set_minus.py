"""Create-set minus-keyword helpers extracted from blueprint.py."""

from __future__ import annotations

import re

_DEPS: dict = {}


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by minus helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


_MINUS_SET_NAME_MARKER = "Минуса общие"  # маркер имени, как у слепков Щербаковой
# Лимиты Директа, символы БЕЗ пробелов (офиц. дока + v5 ref, см. CODER.md):
_MINUS_SHARED_SET_CHAR_BUDGET = 4_096    # библиотечный набор (negativekeywordsharedsets) — как группа
_MINUS_CAMPAIGN_CHAR_BUDGET = 20_000     # минусы НАПРЯМУЮ на кампании (NegativeKeywords кампании)
# Карта механизма привязки минусов по слепку (как в РЕАЛЬНЫХ аккаунтах — live-аудит):
#   campaign   → NegativeKeywords прямо на кампании (≤20 000 симв. без пробелов) — pavlov, kryuchkova
#   shared_set → переиспользовать/создать набор «Минуса общие», привязать через NegativeKeywordSharedSetIds — scherbakova
#   group      → NegativeKeywords на каждой группе объявлений (≤4 096 симв./группа) — terehov
# Default для неизвестного слепка — "group" (безопасно, текущее поведение).
_SLEPOK_MINUS_MODE: dict[str, str] = {
    "pavlov": "campaign",
    "kryuchkova": "campaign",
    "scherbakova": "shared_set",
    "terehov": "group",
    "karavaev": "group",
}


def _collect_pack_minus(slepok: str, site_type: str, tp_code: str) -> list[str]:
    """Собрать ПОЛНЫЙ список минус-фраз из пака M3 для (slepok, site_type, tp_code).

    Обходит все ct-папки пака по данному tp, объединяет {slepok}_minus.txt +
    {slepok}_minus_shared.txt, дедуплицирует (case-insensitive), фильтрует ≤7 слов.
    Возвращает список (не обрезанный по символам).
    """
    key = _SLEPOK_KEY.get((slepok or "").lower(), (slepok or "").lower())
    pack = kp.gather(key, site_type, tp_code)  # {ctNNNN: {"minus":[...]}}
    seen: set[str] = set()
    result: list[str] = []
    for ct_data in pack.values():
        for w in (ct_data.get("minus") or []):
            w = re.sub(r"\s+", " ", str(w).strip())
            if not w or len(w.split()) > 7:
                continue
            k = w.lower()
            if k not in seen:
                seen.add(k)
                result.append(w)
    return result


def _minus_char_budget(words: list[str], budget: int = _MINUS_CAMPAIGN_CHAR_BUDGET) -> list[str]:
    """Обрезать список минус-фраз по символьному бюджету (БЕЗ пробелов).

    Директ считает символы каждой фразы без пробелов (официальная дока).
    Добавляем фразы пока сумма не превысит бюджет.
    """
    total, out = 0, []
    for w in words:
        cost = len(w.replace(" ", ""))
        if total + cost > budget:
            break
        total += cost
        out.append(w)
    return out


def _get_or_create_minus_set(token: str, login: str,
                              slepok: str, site_type: str, tp_code: str) -> int | None:
    """Вернуть id shared минус-набора для tp2/tp4 (зеркалит путь tp1/tp5).

    1. Берём существующий набор «Минуса общие» из аккаунта — КАК ДЕЛАЮТ tp1/tp5
       (_tp5_account_data: next(...'Минуса общие'..., msets[0][0])).
       Если есть — возвращаем сразу, без чтения пака.
    2. Если аккаунт пуст (нет ни одного набора) — собираем минусы из пака M3
       (все ct данного tp, объединить+дедуп), обрезаем по 20 000 симв. без пробелов,
       создаём новый набор через v5 negativekeywordsharedsets.add.
    3. None при любой ошибке (не валит создание кампании).
    """
    try:
        jm = _v5_get("negativekeywordsharedsets", token, login, ["Id", "Name"])
        msets = [(s["Id"], s.get("Name") or "")
                 for s in (jm.get("result") or {}).get("NegativeKeywordSharedSets", [])]
        # Путь tp1/tp5: берём набор с «Минуса общие» в имени, иначе первый из списка
        minus_set = next((mid for mid, nm in msets if _MINUS_SET_NAME_MARKER in nm),
                         (msets[0][0] if msets else None))
        if minus_set:
            return minus_set
        # Аккаунт без shared-set: создаём из пака M3
        words = _collect_pack_minus(slepok, site_type, tp_code)
        words = _minus_char_budget(words, _MINUS_SHARED_SET_CHAR_BUDGET)  # набор ≤4096, не 20000
        if not words:
            return None
        j_add = _v5_call("negativekeywordsharedsets", "add", token, login, {
            "NegativeKeywordSharedSets": [{
                "Name": f"{_MINUS_SET_NAME_MARKER} {tp_code}",
                "NegativeKeywords": words,
            }]
        })
        add_res = (j_add.get("result") or {}).get("AddResults", [])
        new_id = (add_res[0].get("Id") if add_res else None)
        return new_id or None
    except Exception:  # noqa: BLE001 — мягкая деградация, не валим кампанию
        return None


def _attach_minus_set_to_text_campaign(token: str, login: str,
                                        campaign_id: int, minus_set_id: int) -> str | None:
    """Привязать shared минус-набор к v5 TEXT_CAMPAIGN через campaigns.update.

    NegativeKeywordSharedSetIds — поле верхнего уровня кампании (не внутри TextCampaign).
    Возвращает None при успехе, текст ошибки при неудаче.
    """
    try:
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywordSharedSetIds": {"Items": [int(minus_set_id)]},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


def _apply_campaign_direct_minus(token: str, login: str,
                                  campaign_id: int,
                                  slepok: str, site_type: str, tp_code: str) -> str | None:
    """Повесить минусы campaign-direct (pavlov/kryuchkova) напрямую на кампанию.

    Механизм: campaigns.update с NegativeKeywords: {"Items": [...]}.
    Лимит: ≤20 000 символов без пробелов (NegativeKeywords кампании, офиц. дока).
    Мягкая деградация: при ошибке возвращает текст ошибки, кампанию НЕ откатывает.
    Возвращает None при успехе, строку ошибки при неудаче.
    """
    try:
        words = _collect_pack_minus(slepok, site_type, tp_code)
        words = _minus_char_budget(words, _MINUS_CAMPAIGN_CHAR_BUDGET)  # ≤20 000 симв.
        if not words:
            return "нет минусов в паке (campaign-direct пропущен)"
        j = _v5_call("campaigns", "update", token, login, {
            "Campaigns": [{
                "Id": campaign_id,
                "NegativeKeywords": {"Items": words},
            }]
        })
        upd_res = (j.get("result") or {}).get("UpdateResults", [])
        errs = (upd_res[0].get("Errors") or []) if upd_res else []
        if errs:
            return "; ".join(e.get("Message") or e.get("Details") or str(e) for e in errs)
        if "error" in j:
            return _v5_err(j)
        return None  # успех
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]

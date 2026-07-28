"""Аудитории структуры слепка (tp1/tp2/tp4/tp5) → групповые retargetings Grid.

Формат поля взят ДОСЛОВНО из билдера интерфейса Директа (HAR `direct.yandex.ru.73har.har`,
чанк `b10fd987c1079081.chunk.js`)::

    retargetings: contextRetargetings.map(e => ({retCondId: e.id, id: e.retargetingId}))

где ``retCondId`` — id УСЛОВИЯ ретаргетинга (== v5 ``RetargetingListId`` == Grid
``retargetingConditionId``), а ``id`` — id связки группа↔условие: **null для новой связки**.
``searchRetargetings`` — то же самое поле, но для ПОИСКА (tp2/tp4); ``retargetings`` — для
сети (tp1/tp5). Оба поля перезаписываются целиком: пустой список = у группы аудиторий нет.

Что НЕ трогаем (решено по HAR, не угадано):
  • ``audienceTargeting`` остаётся ``ALL_AUDIENCE`` — аудитории живут в ``retargetings``,
    а ``NEW_AUDIENCE``/``NEW_CUSTOMERS`` требуют доп. поля ``currentAudience``;
  • ``retargetingCondition`` остаётся ``None`` — это отдельный inline-путь для
    INTERESTS/HOST/APPLICATION (у нас он идёт через UAC в tp6/tp7, `uac_client.py`).

Резолв id: условия принадлежат аккаунту-ДОНОРУ (из которого снят слепок), поэтому перед
записью каждый id сверяется с ЦЕЛЕВЫМ кабинетом (карта ``{имя условия → id}`` из v5
``retargetinglists.get``, уже читается один раз на джобу в
``create_set_orchestrator`` → :func:`remember_account_conditions`). Ненайденные НЕ
отправляются, а попадают в видимый warning позиции.
"""
from __future__ import annotations

import re
import threading

# Разделы структуры, которые несут id УСЛОВИЯ ретаргетинга. Проверено 2026-07-28 по всем
# `slepki/*.json`: в tp1/tp2/tp4/tp5 встречаются только `AUDIENCE:`/`RETARGETING:` (1603/933)
# и 4 голых id; все восьмизначные из диапазона 40–41 млн — тот же диапазон, что
# `retargetingConditionId` в HAR. Префикс — ярлык раздела UI, сущность одна.
# `INTERESTS:`/`HOST:`/`APPLICATION:` — это goal id (10–11 знаков), ДРУГАЯ сущность
# (inline `retargetingCondition`, путь tp6/tp7 UAC) — сюда не берём.
_COND_PREFIXES = ("AUDIENCE", "RETARGETING")

_ACC_LOCK = threading.Lock()
# login → {"by_name": {норм. имя: [id, ...]}, "ids": {id, ...}} условий ЦЕЛЕВОГО кабинета.
_ACCOUNT_CONDITIONS: dict[str, dict] = {}

# Каналы кампаний набора, если spec/mode до билдера не доехали (фолбэк, НЕ основной путь).
# Единственный сетевой канал — tp1 (`automation_runtime._PLATFORMS_RSYA`: network=True/search=False,
# `create_set_tp1_builders.py:2454` spec network=True). tp2/tp4 — `_PLATFORMS_SEARCH_ONLY`
# (`create_set_feed_builders.py:174` search=True/network=False), tp3/tp5 — тоже Search-канал
# (`create_set_feed_builders.py:607` «tp3 и tp5 — оба Search-канал», spec :613).
_NETWORK_TP_CODES = frozenset({"tp1"})


def is_search_channel(campaign_spec: dict | None = None, mode: str = "",
                      tp_code: str = "") -> bool:
    """Канал кампании → в какое поле группы едут аудитории.

    Поиск → ``searchRetargetings``; сеть (РСЯ) → ``retargetings`` (HAR-разбор в шапке модуля).
    Признак берётся от КАМПАНИИ, а не от списка tp-кодов: tp5 — поисковая кампания
    (`create_set_feed_builders.py:607-613`), и по tp-списку `("tp2","tp4")` её аудитории
    уезжали в сетевое поле.

    Источники по убыванию авторитетности:
      1. ``campaign_spec`` с ключами ``search``/``network`` — ровно как `grid_create.create_full:936`
         (куки/Grid-путь: `search_only = search and not network`);
      2. ``mode`` спеки v501 — `cmc.UnifiedCampaignSpec.mode` у tp1 (`network_cpa`/`network_payconv`)
         и `_create_search_test_campaign(mode=…)` у tp5 (`search`): префикс `network` → сеть;
      3. ``tp_code`` — фолбэк по каналу tp (см. `_NETWORK_TP_CODES`), когда ни spec, ни mode не дошли.
    """
    if isinstance(campaign_spec, dict) and (
            "search" in campaign_spec or "network" in campaign_spec):
        return bool(campaign_spec.get("search")) and not bool(campaign_spec.get("network"))
    m = str(mode or "").strip().lower()
    if m:
        return not m.startswith("network")
    return str(tp_code or "").strip().lower() not in _NETWORK_TP_CODES


def _norm_name(name) -> str:
    """Ключ матчинга имени условия: NBSP→пробел, схлопнутые пробелы, casefold.

    В структуре живут оба варианта «Интересы и\xa0привычки» и «Интересы и привычки» —
    без нормализации NBSP они не совпали бы с именем того же условия в кабинете.
    """
    s = str(name or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().casefold()


def remember_account_conditions(login: str, ret_map: dict | None) -> None:
    """Запомнить условия ретаргетинга ЦЕЛЕВОГО кабинета: ``{имя условия → id}``.

    Источник — `create_set_corrections._account_retargeting` (v5 ``retargetinglists.get``),
    который уже читается один раз на джобу в `create_set_orchestrator`. Пустая карта не
    кэшируется: «кабинет не прочитан» и «в кабинете нет условий» должны различаться —
    в первом случае резолвер обязан честно предупредить, а не молча отправить пусто.
    """
    login = str(login or "").strip()
    if not login or not ret_map:
        return
    by_name: dict[str, list[str]] = {}
    ids: set[str] = set()
    for nm, cid in dict(ret_map).items():
        cid_s = str(cid or "").strip()
        if not cid_s:
            continue
        ids.add(cid_s)
        key = _norm_name(nm)
        if key:
            bucket = by_name.setdefault(key, [])
            if cid_s not in bucket:
                bucket.append(cid_s)
    if not ids:
        return
    with _ACC_LOCK:
        _ACCOUNT_CONDITIONS[login] = {"by_name": by_name, "ids": ids}


def account_conditions(login: str) -> dict | None:
    """Условия целевого кабинета или None, если карта для логина не прочитана."""
    with _ACC_LOCK:
        return _ACCOUNT_CONDITIONS.get(str(login or "").strip())


def forget_account_conditions(login: str | None = None) -> None:
    """Сбросить кэш (login=None → весь). Нужен тестам и повторным прогонам."""
    with _ACC_LOCK:
        if login is None:
            _ACCOUNT_CONDITIONS.clear()
        else:
            _ACCOUNT_CONDITIONS.pop(str(login).strip(), None)


def struct_audience_pairs(item: dict) -> list[tuple[str, str]]:
    """``item`` структуры → [(id условия донора, имя условия)] в порядке появления.

    Имя берём из соседнего поля ``rl_audiences`` ([{rl_id, name, type}]) — это единственный
    устойчивый признак для матчинга в чужом кабинете. Имени нет → пустая строка (резолв
    возможен только точным совпадением id).
    """
    names: dict[str, str] = {}
    for r in (item.get("rl_audiences") or []):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("rl_id") or "").strip()
        nm = str(r.get("name") or "").strip()
        if rid and nm:
            names[rid] = nm
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (item.get("audiences") or []):
        s = str(raw or "").strip()
        if not s:
            continue
        head, sep, tail = s.partition(":")
        if sep:
            if head.strip().upper() not in _COND_PREFIXES:
                continue                       # INTERESTS/HOST/APPLICATION — другая сущность
            cid = tail.strip()
        else:
            cid = head.strip()
        if not cid.isdigit() or cid in seen:
            continue
        seen.add(cid)
        out.append((cid, names.get(cid, "")))
    return out


def struct_audiences_by_gk(slepok: str, site_type: str, tp_code: str) -> dict[str, list[tuple[str, str]]]:
    """``{gk группы → [(id, имя), ...]}`` по структуре слепка для одного tp.

    Ключ — ТОЛЬКО ``gk`` (слуг группы): по ct аудитории не разносим, потому что один ct
    покрывает много групп и «на всякий случай» проставил бы чужие аудитории. Группа без
    ``gk`` аудиторий не получает.

    Своего кэша нет намеренно: чтение идёт через `create_set_structure._load_struct`
    (`slepki_store.assemble`), у которого кэш по сигнатуре mtime+size частей — иначе правка
    слепка не подхватилась бы до рестарта воркера.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        from .create_set_structure import (_gk_of, _iter_items, _load_struct,
                                           _slepok_key)
        d = _load_struct()
        key = _slepok_key(slepok)
        dl = next((x for x in (d.get("directologists") or []) if x.get("key") == key), None)
        st = next((s for s in ((dl.get("site_types") or []) if dl else [])
                   if s.get("name") == site_type), None)
        for tp in ((st.get("tp") or []) if st else []):
            if (tp.get("code") or "").strip() != tp_code:
                continue
            for _lbl, it in _iter_items(tp):
                gk = (_gk_of(it) or "").strip()
                if not gk:
                    continue
                pairs = struct_audience_pairs(it)
                if not pairs:
                    continue
                bucket = out.setdefault(gk, [])
                for p in pairs:
                    if p not in bucket:
                        bucket.append(p)
    except Exception as exc:  # noqa: BLE001 — чтение структуры не должно ронять создание
        # НЕ молчим: сбой чтения структуры = ТИХАЯ потеря ВСЕХ аудиторий позиции. Раньше здесь
        # был голый `out = {}` без единой строки в логе — падение выглядело как «аудиторий нет».
        out = {}
        _msg = (f"[audiences] ОШИБКА чтения структуры {slepok}/{site_type}/{tp_code}: "
                f"{type(exc).__name__}: {str(exc)[:200]} — аудитории этого tp НЕ будут проставлены")
        print(_msg, flush=True)
        import logging as _alog
        _alog.getLogger("direct.audiences").exception(_msg)
    return out


def struct_audience_group_count(slepok: str, site_type: str,
                                tp_codes=("tp1", "tp2", "tp4", "tp5")) -> int:
    """Сколько групп структуры несут аудитории (по всем ``tp_codes``).

    Нужен оркестратору, чтобы отличить «в слепке аудиторий нет» от «аудитории есть, но карта
    условий кабинета не прочитана» — во втором случае джоба обязана быть НЕ зелёной.
    """
    total = 0
    for tp in (tp_codes or ()):
        total += len(struct_audiences_by_gk(slepok, site_type, str(tp)) or {})
    return total


def resolve_for_account(login: str, pairs) -> tuple[list[str], list[str]]:
    """(id для ЦЕЛЕВОГО кабинета, видимые предупреждения).

    Порядок резолва:
      1. id донора существует в целевом кабинете → берём как есть (обычный случай —
         слепок снят с того же аккаунта, куда создаём);
      2. иначе матч по ИМЕНИ условия (нормализованному) → id целевого кабинета;
         несколько условий с одинаковым именем → берём наименьший id и предупреждаем;
      3. иначе id НЕ отправляется и попадает в предупреждение.
    Карта кабинета не прочитана вовсе → не отправляем ничего + предупреждение.
    """
    pairs = list(pairs or [])
    if not pairs:
        return [], []
    acc = account_conditions(login)
    if not acc:
        return [], [f"аудитории не проставлены ({len(pairs)} шт.): "
                    f"карта условий ретаргетинга кабинета {login} не прочитана"]
    ids: list[str] = []
    notes: list[str] = []
    for cid, nm in pairs:
        cid = str(cid or "").strip()
        if not cid:
            continue
        if cid in acc["ids"]:
            if cid not in ids:
                ids.append(cid)
            continue
        cands = acc["by_name"].get(_norm_name(nm)) if nm else None
        if cands:
            tid = sorted(cands, key=lambda x: (len(x), x))[0]
            if tid not in ids:
                ids.append(tid)
            if len(cands) > 1:
                notes.append(f"аудитория «{nm}» (донор {cid}): в кабинете {len(cands)} "
                             f"условий с таким именем — взято {tid}")
        else:
            notes.append(f"аудитория «{nm or 'без имени'}» (донор {cid}) не найдена "
                         f"в кабинете {login} — НЕ отправлена")
    return ids, notes


def retargetings_payload(ids) -> list[dict]:
    """[id условия] → значение поля ``retargetings``/``searchRetargetings``.

    Дословно билдер Директа: ``{retCondId: <id условия>, id: <id связки | null>}``.
    Связка новая → ``id: None``.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for x in (ids or []):
        s = str(x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append({"retCondId": s, "id": None})
    return out


def group_notes(groups) -> list[str]:
    """Собрать уникальные предупреждения аудиторий со всех групп набора."""
    out: list[str] = []
    for g in (groups or []):
        for n in ((g or {}).get("audiences_notes") or []):
            if n not in out:
                out.append(n)
    return out


def attach_to_group(group: dict, login: str, pairs) -> None:
    """Проставить группе резолвнутые ``audiences`` + ``audiences_notes`` (in-place).

    Пустой ``pairs`` → полей не появляется вовсе: группа без аудиторий в структуре обязана
    получить ПУСТОЙ ``retargetings``, как и раньше.
    """
    if not pairs:
        return
    ids, notes = resolve_for_account(login, pairs)
    if ids:
        group["audiences"] = ids
    if notes:
        group["audiences_notes"] = notes
        for n in notes:
            print(f"[audiences] {login}: {n}", flush=True)

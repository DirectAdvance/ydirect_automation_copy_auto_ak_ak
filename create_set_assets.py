"""Create-set creative asset and responsive-ad helpers extracted from blueprint.py."""

from __future__ import annotations

import re
import time                                    # _v5_callout_pool троттлит adextensions.add (стр.687); без импорта — NameError, уточнения молча не привязывались

from .text_norm import _trim_clean, _bad_ad_title as _text_norm_bad_title
from .text_gen import (
    _brand_first_reorder, _brand_isolated_first_phrase,
    _NEUTRAL_CREDIT_TITLES, _NEUTRAL_CREDIT_TEXTS,
)

_DEPS: dict = {}


# ── DI из blueprint (заглушки падают громко, если configure не отработал) ──
# Нужны, потому что `_upgrade_credit_*` — ФИНАЛЬНАЯ сборка адаптива, ПОСЛЕ всех `_cf`:
# хардкод-варианты про «новые авто» иначе уезжают в Б/У-кампании tp1–tp5 мимо всей фильтрации.
def _drop_new_car(*a, **k):
    raise RuntimeError("create_set_assets._drop_new_car не инъектирован (configure)")


def _is_bu_site(*a, **k):
    raise RuntimeError("create_set_assets._is_bu_site не инъектирован (configure)")

MANUAL_CREATIVES_DIR = "/opt/creatives/Manual"


def configure(deps: dict) -> None:
    """Bind blueprint dependencies used by asset helpers."""
    _DEPS.clear()
    _DEPS.update(deps)
    globals().update(deps)


def _manual_creative_paths(ct_code: str) -> list:
    """Manual-креативы для ct как локальные пути на LXC.

    Источник правды теперь тот же, что и у вкладки «Контент»:
    `kontent_pack` индексирует external_assets из M3 (`/Users/Shared/agency/creatives/Manual/...`),
    а здесь мы лениво скачиваем эти файлы в локальный cache через `fetch_remote_asset`.

    Legacy-fallback `/opt/creatives/Manual/{ct}/` оставлен только если такой mount реально есть.

    ⚠️ `/opt/creatives` — НЕ локальный диск, а sshfs-монт M3 (`mount`: m3-relay:/Users/Shared/
    agency/creatives, fuse.sshfs), хотя комментарий ниже исторически звал его «локальным».
    Те же файлы лежат ЛОКАЛЬНО в зеркале `NEURO_PACK_MOUNT/_manual/{ct}/` (ночной
    sync_content_m3.py). Отдавали sshfs-пути → их потом читал upload_image голым fh.read()
    без таймаута → бесконечное зависание потоков (job f58a123d8405 / a4bef725b5cb, 2026-07-18:
    12-14 потоков в fh.read/isfile/realpath, created 3/20). Поэтому: зеркало ПЕРВЫМ,
    sshfs — только фолбэк и только через операции с пределом времени.
    """
    import os as _os
    ct = (ct_code or "").strip().lower()
    if not ct:
        return []
    out: list[str] = []

    # 1) Manual-креативы с диска. Приоритет — ЛОКАЛЬНОЕ зеркало (_manual/{ct}), sshfs-монт
    #    /opt/creatives/Manual — только если в зеркале этого ct нет.
    try:
        _mirror = getattr(kp, "_LOCAL_MIRROR_ROOT", None)
        folder = _os.path.join(_mirror, "_manual", ct) if _mirror else ""
        if not (folder and kp.isdir_bounded(folder)):
            folder = _os.path.join(MANUAL_CREATIVES_DIR, ct)
        if kp.isdir_bounded(folder):
            out.extend(sorted(
                _os.path.join(folder, f)
                for f in kp.listdir_bounded(folder)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ))
    except Exception:  # noqa: BLE001
        pass

    # 2) Актуальный путь: Manual-ассеты из M3 index -> локальный cache path.
    try:
        idx = kp._load_index() or {}
        ext_key = f"Manual|manual|{ct}"
        rows = (idx.get("external_assets") or {}).get(ext_key) or []
        for rec in rows:
            if str(rec.get("kind") or "") != "image_manual":
                continue
            remote = str(rec.get("remote") or "").strip()
            if not remote:
                continue
            local = kp.fetch_remote_asset(remote)
            if local:
                out.append(local)
    except Exception:  # noqa: BLE001
        pass

    return sorted(dict.fromkeys(out))


# ── Анти-блок: широкая структура = десятки-сотни групп на кампанию. Пофайловый цикл
#    (3 вызова/группу) = сотни запросов → риск 429/блокировки. Лечим БАТЧИНГОМ (один
#    adgroups.add берёт до 1000 групп) + паузами между пачками + капом групп за проход.
_AC_GROUP_CAP = 150           # макс. групп на кампанию за один проход (остальное → deferred)
_AC_CHUNK_AG = 100            # групп в одном adgroups.add
_AC_CHUNK_KW = 1000           # ключей в одном keywords.add
_AC_CHUNK_AD = 100            # объявлений в одном ads.add
_AC_BATCH_SLEEP = 0.2         # пауза между батч-вызовами (троттл, сек); A4: 0.4→0.2, при 429
                              # срабатывают существующие backoff-ретраи в самих батч-вызовах
# Комбинаторное объявление (RESPONSIVE_AD) — замена ТГО (TextAd), которое отключают с 30.06.2026.
# Создаётся ТОЛЬКО через v501 ads.add {ResponsiveAd:{Titles[],Texts[],Href,AdImageHashes[],...}}.
# Несколько заголовков/текстов в ОДНОМ объявлении (Яндекс комбинирует). Уточнения наследуются
# на уровне группы/кампании (поле AdExtensions у ResponsiveAd НЕ поддерживается).
_RA_TITLE_MAX = 56            # лимит длины заголовка
_RA_TEXT_MAX = 81            # лимит длины текста
_RA_TITLES_CAP = 7           # макс. заголовков в комбинаторном (как в UI Директа «… из 7»)
_RA_TEXTS_CAP = 3            # макс. текстов в комбинаторном (Яндекс: Texts от 1 до 3 — 5 = ошибка ads.add)

# «Текст по умолчанию» для ShoppingAd/ListingAd (динамические/каталожные группы tp3/tp5).
# Единый переиспользуемый текст на все группы (решение Семёна 2026-07-10, обновлён 2026-07-10 Д5):
# — НЕ per-group (не брать texts[0] из AI — там может быть аварийный короткий fallback);
# — заполнен под лимит ≤81 символа;
# — ⚠️ НЕ содержит слов кредит/автокредит/взнос/платёж/одобрение банка (Д5 2026-07-10):
#   в товарных/каталожных объявлениях (ShoppingAd) кредитная лексика запрещена.
#   Акцент — продукт (автомобили в наличии, выбор, скидка, тест-драйв).
SHOPPING_DEFAULT_TEXT = "Новые автомобили в наличии. Большой выбор. Скидки до 45% при покупке. Тест-драйв."


def _dedup_cap(items, maxlen: int, cap: int) -> list:
    """Обрезать по длине, выкинуть пустые/дубли, ограничить количеством. Дедуп — по
    НОРМАЛИЗОВАННОМУ ключу (_variant_norm_key: числа схлопнуты), чтобы «…стоянку - 45%» и
    «…стоянку - 40%» не уходили оба в одно объявление (Комбинаторное: ≤5 заголовков / ≤3 текста)."""
    out: list = []
    seen: set = set()
    for it in items or []:
        # Обрезка по слову + чистка оборванного хвоста (_trim_ad_line), а не жёсткий срез [:maxlen].
        s = _trim_ad_line(it, maxlen)
        if not s:
            continue
        k = _variant_norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _combo_fill_titles(items: list, cap: int = _RA_TITLES_CAP) -> list:
    """Добор заголовков ResponsiveAd до 7 без ломания бренда/модели в первом заголовке."""
    src = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    out = list(src)
    anchor = (src[0].split(".")[0].strip() if src else "Авто в кредит").rstrip(" ,.")
    if len(anchor) > 34:
        sp = anchor[:34].rfind(" ")
        anchor = anchor[:sp if sp > 15 else 34].rstrip(" ,.")
    tails = [
        "Кредит от 9 000 ₽/мес",
        "Одобрение за 30 минут",
        "КАСКО в подарок",
        "Трейд-ин выше рынка",
        "Первый взнос 0 ₽",
        "Господдержка на авто",
        "15 банков-партнеров",
        "Авто в наличии",
    ]
    for tail in tails:
        if len(out) >= cap:
            break
        cand = f"{anchor}. {tail}" if anchor else tail
        if len(cand) > _RA_TITLE_MAX:
            cand = f"{anchor} {tail}"
        if len(cand) > _RA_TITLE_MAX:
            cand = _trim_clean(cand, _RA_TITLE_MAX)   # по слову + чистка хвоста («…за 30»)
        # Правило Семёна: свободно ≤2 симв. Если кандидат короче hi-2 — добиваем хвостами.
        if cand and len(cand) < _RA_TITLE_MAX - 2:
            cand = _fill_title(cand, _RA_TITLE_MAX - 2, _RA_TITLE_MAX)
        if cand and cand not in out:
            out.append(cand)
    return out


def _combo_fill_texts(items: list, cap: int = _RA_TEXTS_CAP) -> list:
    """Добор текстов ResponsiveAd до 3, с разными УТП и длиной до 81.
    #26: используем _GENERIC_TEXT_FILLERS (76-81 симв) вместо коротких строк;
    _trim_to_word вместо [:max].rsplit — не срезает последнее слово у коротких текстов."""
    out = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    for cand in _GENERIC_TEXT_FILLERS:
        if len(out) >= cap:
            break
        cand = _trim_to_word(cand, _RA_TEXT_MAX).rstrip(" ,.")
        if cand and cand not in out:
            out.append(cand)
    return out[:cap]   # seed из items мог уже быть >cap — держим контракт (ads.add: Texts ≤3)


def _credit_title_bucket(s: str) -> str:
    x = (s or "").lower()
    if re.search(r"9\s*000|/мес|плат[её]ж", x):
        return "payment"
    if re.search(r"0\s*₽|0\s*руб|первый\s+взнос", x):
        return "first_payment"
    if re.search(r"каско|1\s*год", x):
        return "kasko"
    if re.search(r"30\s*мин|одобр", x):
        return "approval"
    if re.search(r"15\s*банк|банк", x):
        return "banks"
    if re.search(r"45\s*%|скидк|выгод", x):
        return "discount"
    if re.search(r"150\s*%|трейд", x):
        return "tradein"
    if re.search(r"2026|госпрограмм|господдерж", x):
        return "state"
    if re.search(r"1\s*мин|заявк", x):
        return "apply"
    return "other"


def _credit_title_anchor(items: list[str]) -> tuple[str, str]:
    first = str((items or [""])[0] or "").strip()
    anchor = (first.split(".")[0].strip() if first else "Авто в кредит").rstrip(" ,.")
    anchor = re.sub(r"(?i)^(кредит\s+на|купить)\s+", "", anchor).strip()
    anchor = re.sub(r"(?i)\s+в\s+кредит\b", "", anchor).strip()
    brand = anchor
    m = re.match(r"(.+?)\s+в\s+[А-ЯA-ZЁ0-9]", anchor)
    if m:
        brand = m.group(1).strip()
    brand = brand.rstrip(" ,.")
    return anchor, brand or anchor


def _valid_pack_brand_name(ct: str, raw_name: str) -> str:
    name = str(raw_name or "").strip()
    low = name.lower()
    if (ct or "").strip().lower() == "ct0000":
        return ""
    if not name:
        return ""
    if low.startswith("кластер запросов не определен") or low == "полное отсутствие ключей":
        return ""
    if not _coder_name_real_brand(name):   # «Авито»/«Дром»/«Автосалон» (сегмент Общее) — НЕ марка:
        return ""                          # иначе _brand_text_set лепит «Купить Авито в кредит» в тексты
    return name


def _pack_group_display_name(ct: str, raw_name: str, brand: str = "") -> str:
    """Человекочитаемый суффикс имени группы. Для общих ct бренд остаётся пустым
    (чтобы не попасть в тексты/фильтры), но в имени группы показываем тему."""
    b = str(brand or "").strip()
    if b:
        return b
    c = (ct or "").strip().lower()
    name = str(raw_name or "").strip()
    low = name.lower()
    if c == "ct0000" or low == "полное отсутствие ключей":
        return "Общая"
    if low.startswith("кластер запросов не определен"):
        return "Общая"
    return name or "Общая"


def _trim_ad_line(s: str, maxlen: int) -> str:
    s = str(s or "").strip()
    if len(s) <= maxlen:
        return s
    # _trim_clean: обрезка по слову + чистка оборванного хвоста («Одобрение за 30» —
    # live-кейс psm/ozge 2026-07-05); единая реализация в text_norm.
    return _trim_clean(s, maxlen)


_DANGLING_TEXT_TAIL_RE = re.compile(
    r"(?i)(?:\s+|^)(?:и|в|во|на|по|при|с|со|для|от|без|"
    r"каско\s+на\s+1|каско\s+на|первый\s+взнос|одобрение\s+за|платеж\s+от)\s*$"
)


def _finalize_text_line(s: str, maxlen: int = _RA_TEXT_MAX, minlen: int = 79) -> str:
    """Clean generated ad text: no dangling tails and no large unused character budget."""
    line = _trim_ad_line(s, maxlen).rstrip(" ,.")
    while True:
        m = _DANGLING_TEXT_TAIL_RE.search(line)
        if not m:
            break
        head = line[:m.start()].rstrip(" ,.;:-")
        if len(head) < 35:
            break
        line = head
    if len(line) >= minlen:
        return line
    for tail in ("Звоните", "Звоните!", "Оставьте заявку.", "Узнайте условия."):
        if re.search(r"(?i)(?<![а-яёa-z])" + re.escape(tail.rstrip("!.")), line):
            continue      # дедуп по границе слова: иначе «Звоните. Звоните!» (тексты 53-63 симв)
        sep = " " if line.endswith((".", "!", "?")) else ". "
        cand = f"{line}{sep}{tail}".strip()
        if len(cand) <= maxlen:
            line = cand
            if len(line) >= minlen:
                break
    return line


_DMP_B2B_TITLE_RE = re.compile(
    r"(?i)(лид|заявк|контакт|клиент|компани|бизнес|источник|идентифик|горяч)"
)


def _needs_credit_title_upgrade(items: list[str]) -> bool:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not seq:
        return True
    # B2B-лидоген (dmp): если хоть один заголовок содержит B2B-маркер — апгрейд авто-кредитными
    # заголовками не нужен и вреден (навесит «Автокредит от 9000₽/мес» на B2B-контент).
    if any(_DMP_B2B_TITLE_RE.search(t) for t in seq):
        return False
    buckets = {_credit_title_bucket(x) for x in seq}
    first_words = [x.split()[0].lower().rstrip(".,!?") for x in seq if x.split()]
    # Brand-first (DoD §2.1): при брендовых кампаниях все 7 заголовков ОБЯЗАНЫ начинаться с бренда
    # (напр. «BAIC …»), поэтому same_prefix=5–7 — это НОРМА, а не дефект.
    # Определяем «бренд-слово»: первое слово, которое встречается 4+ раза И НЕ является
    # типовым авто/кредит-словом (Авт*, Нов*, Купи*, Кредит, Платеж, Выгода…).
    _GENERIC_FW_RE = re.compile(
        r"(?i)^(авт|нов|купит|приобрет|выгод|одобр|кредит|платеж|трейд|каско|зака)")
    _brand_fw: set[str] = {
        w for w in set(first_words)
        if first_words.count(w) >= 4 and not _GENERIC_FW_RE.match(w)
    }
    if _brand_fw:
        # Brand-first slepok content (DoD §2.1): numeric и bucket-diversity — НЕ валидные
        # сигналы качества для голоса слепка. Кrючкова пишет органично без цифр («Распродаём
        # склад. Цены снижены», «Срочно! Последние авто по старой цене») и их phrases маппятся
        # в bucket="other" (не в кредитный). «other»-phrases — это ЕСТЬслепок-голос, НЕ признак
        # generic-вывода. Единственный разумный guard здесь: если НЕ-бренд первые слова ТОЖЕ
        # повторяются (напр. «BAIC. Кредит. Условия» × 4 + «BAIC. Кредит. Условия» × 4) —
        # это действительно generic. Иначе возвращаем False (upgrade НЕ нужен).
        non_brand_fw = [w for w in first_words if w not in _brand_fw]
        same_prefix_nb = max((non_brand_fw.count(w) for w in set(non_brand_fw)), default=0) if non_brand_fw else 0
        # Dedup-guard: даже brand-first голос обязан быть РАЗНООБРАЗНЫМ. Если LLM выдал
        # почти идентичные brand-first заголовки (напр. 7× «BAIC. Кредит. Условия»),
        # non_brand_fw пуст → same_prefix_nb=0 → сломанный набор проскочил бы как «голос».
        # Ловим по числу УНИКАЛЬНЫХ заголовков: <3 distinct = не голос, а вырожденный вывод.
        # Разнообразный набор distinct-заголовков (≥3) остаётся False → голос сохраняется.
        distinct_titles = len({t.strip().lower() for t in seq})
        if distinct_titles < 3:
            return True
        return same_prefix_nb >= 4
    # Non-brand-first: полная проверка (regression-guard для общего AI-вывода)
    same_prefix = max((first_words.count(w) for w in set(first_words)), default=0)
    missing_numbers = sum(1 for x in seq if not re.search(r"\d", x))
    return len(buckets - {"other"}) < 5 or same_prefix >= 4 or missing_numbers > 0


def _upgrade_credit_titles(items: list[str], cap: int = _RA_TITLES_CAP,
                           site_type: str = "") -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    # ПОСЛЕДНИЙ РУБЕЖ по типу сайта. Раньше `_drop_new_car` (стр. ниже) чистил только СВОЙ
    # хардкод `variants`, а входящий `seq` уходил в live как есть — и early-return, и цикл
    # `variants + seq` протаскивали «новые авто» на Б/У. Режем `seq` ДО ветвления: обе ветки
    # теперь отдают набор без новой-авто-лексики. На не-Б/У — no-op. Если фильтр опустошил seq,
    # `_needs_credit_title_upgrade([])` → True → апгрейд с нейтральным floor (пустым не выйдет).
    seq = _drop_new_car(seq, site_type)
    if not _needs_credit_title_upgrade(seq):
        # Safety-net: даже в early-return прогоняем каждый через стоп-фильтр —
        # AI мог сгенерировать «До 150% цены авто» в иначе разнообразном наборе.
        return [t for t in seq if not _text_norm_bad_title(t)][:cap]
    anchor, brand = _credit_title_anchor(seq)
    brand_low = (brand or "").strip().lower()
    brand_real = bool(brand) and not brand_low.startswith("авто")
    if not brand_real:
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽",
            "Купить новое авто. КАСКО на 1 год бесплатно",
            "Платеж от 9 000 ₽/мес. Новые авто в наличии",
            "Одобрение за 30 минут онлайн. Новые авто",
            "Кредит от 15 банков онлайн. Подбор авто",
            "Выгода до 45% на новые авто. Узнайте условия",
            "Трейд-ин выше рынка. Оценка онлайн",
            "Госпрограмма 2026. Кредит на новые авто",
        ]
    else:
        # §2.1 brand-first: марка/модель строго В НАЧАЛЕ, до первой точки.
        # §2.1-var: ВАРИАТИВНОСТЬ захода — первые ~18 символов разные у разных заголовков.
        # Не лепить «{Brand} в {Город}.» на ВСЕ 7: используем {anchor} (с городом),
        # {brand} (без города), «Купить {brand}» и «{brand} в кредит» вперемешку.
        # Все варианты brand-first: бренд/модель до первой точки.
        #
        # Д1 (2026-07-10) — авто-контекст: позиции 1 и 4 (0-индекс) несут УТП «авто в наличии» /
        # «новые автомобили» — 2 из 7 итоговых заголовков явно сообщают, что продаём МАШИНЫ,
        # а не только финансовый продукт. Остальные 5 — кредитный угол по DoD §2.
        # Позиции 7–10 — резерв (при cap=7 в итог не попадают, но страхуют от дедупа).
        # §2.1-integ (2026-07-10): бренд ИНТЕГРИРОВАН в первую фразу — НЕ изолирован.
        # ДЕФЕКТ: «Belgee. Авто в наличии» (brand + точка + рублёный УТП) = НЕПРИЕМЛЕМО.
        # НОРМА: «Belgee в наличии», «Новый Belgee», «Купить Belgee в кредит» — brand в составе фразы.
        variants = [
            f"{brand} в кредит. Первый взнос 0 ₽",                    # 0: brand интегрирован [OK]
            f"{brand} в наличии. Кредит от 9 000 ₽/мес",              # 1: AUTO-CONTEXT, brand интегр. (исп. «{brand}. Авто в наличии…»)
            f"{brand} с платежом от 9 000 ₽/мес",                     # 2: brand интегрирован [OK]
            f"Купить {brand} в кредит. Одобрение за 30 мин",          # 3: action+brand интегр. [OK]
            # 4: AUTO-CONTEXT. На Б/У «Новый {brand}» ВРЁТ про товар, а `_drop_new_car` это не
            # снимет — `_NEW_RE` требует хвост «авто»/«автомобил», здесь же МАРКА («Новый Haval»).
            # Поэтому подменяем по site_type явно, а не фильтром.
            (f"{brand} с пробегом. Выгода до 45%" if _is_bu_site(site_type)
             else f"Новый {brand}. Выгода до 45%"),
            f"{brand} с выгодой до 45% при покупке",                  # 5: brand интегрирован [OK]
            f"{brand} трейд-ин. Оценка онлайн",                         # 6: brand интегр. (был «До 150% цены авто» — inflated, убран 2026-07-15)
            f"{brand} и КАСКО на 1 год бесплатно",                    # 7: brand интегр., резерв (исп. «{brand}. КАСКО…»)
            f"{brand} в кредит. Кредит от 15 банков",                 # 8: резерв [OK]
            f"{brand} по госпрограмме 2026. Кредит",                  # 9: brand интегр., резерв (исп. «{brand}. Госпрограмма…»)
            f"Купить {brand}. Заявка за 1 минуту",                    # 10: action+brand интегр. [OK]
        ]
    # Б/У-сайт: выкинуть хардкод-варианты про НОВЫЕ авто («Новые авто в кредит…», «Купить новое
    # авто…», «Выгода до 45% на новые авто…»). Это ПОСЛЕДНЯЯ точка, где их ещё можно снять:
    # `_upgrade_credit_titles` работает после всех `_cf`, и её результат уходит прямо в ads.add.
    # На сайтах НОВЫХ авто `_drop_new_car` — no-op (`_is_bu_site` ложно), набор не меняется.
    variants = _drop_new_car(variants, site_type)
    # Floor: в non-brand-ветке фильтр срезает 6 из 8 вариантов — добираем нейтральным пулом
    # (он не режется ни `_drop_used_car`, ни `_drop_new_car`), чтобы комплект не просел.
    # Добавляем ВСЕГДА в хвост: дедуп ниже и `cap` отбросят лишнее, на не-Б/У порядок не меняется.
    variants = variants + _drop_new_car(list(_NEUTRAL_CREDIT_TITLES), site_type)
    _fill = globals().get("_fill_title")
    out: list[str] = []
    seen: set[str] = set()
    for cand in variants + seq:
        line = _trim_ad_line(cand, _RA_TITLE_MAX)
        if not line:
            continue
        # brand-first детерминированно (страховка для pass-through seq: «Платеж … BAIC» → «BAIC. …»)
        if brand_real:
            line = _trim_ad_line(_brand_first_reorder(line, brand), _RA_TITLE_MAX)
            if _brand_isolated_first_phrase(line, brand):
                continue
        # §2.2 короткие: добить до ≥ hi-2 (54), чтобы не оставлять 13-18 свободных символов
        if callable(_fill) and line and len(line) < _RA_TITLE_MAX - 2:
            line = _trim_ad_line(_fill(line, _RA_TITLE_MAX - 2, _RA_TITLE_MAX), _RA_TITLE_MAX)
            if brand_real and _brand_isolated_first_phrase(line, brand):
                continue
        if not line:
            continue
        # Safety-net: стоп-фразы «нельзя в live» (трейд-ин+big%, без документов и др.) не пропускаем
        # даже если они вошли из хардкод-вариантов (защита на случай расхождения шаблонов и стоп-листа).
        if _text_norm_bad_title(line):
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    return out



# Паттерн для детекции УТП-фразы вместо настоящего бренда в первом предложении текста.
# «Первый взнос 0 ₽», «Платёж от 9 000 ₽» и т.п. содержат цифры или ключевые УТП-слова.
# Если _credit_title_anchor вернул такой «brand», шаблон f"Кредит на {brand}. Первый взнос 0 ₽..."
# приводит к тавтологии («Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽…»). → force non-brand ветку.
_UTP_ANCHOR_RE = re.compile(r"(?i)\d|взнос|платёж|платеж|одобрени|выгода|каско|скидк")


def _has_dup_clause(text: str) -> bool:
    """True если в тексте есть хотя бы два одинаковых предложения (split по '. ', '! ', '? ')."""
    parts = [p.strip().lower() for p in re.split(r"[.!?]\s+", text) if p.strip()]
    return len(parts) != len(set(parts))


def _upgrade_credit_texts(items: list[str], cap: int = _RA_TEXTS_CAP,
                          site_type: str = "") -> list[str]:
    seq = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    # Последний рубеж по типу сайта: входящий seq мог принести «новые авто» на Б/У (сырой
    # generic-фолбэк выше `_cf`). Режем ДО цикла `seq + variants`; на не-Б/У — no-op. Опустел —
    # `variants` (хардкод + `_NEUTRAL_CREDIT_TEXTS`) добьют комплект, пустым набор не станет.
    seq = _drop_new_car(seq, site_type)
    anchor, brand = _credit_title_anchor(seq or ["Авто в кредит"])
    brand_low = (brand or "").strip().lower()
    # Если «brand» — это УТП-фраза (содержит цифры / слова взнос/платёж/КАСКО/…), а не марка авто,
    # использовать non-brand ветку шаблонов (иначе f"Кредит на {brand}. Первый взнос 0 ₽. …" → тавтология).
    brand_is_utp = _UTP_ANCHOR_RE.search(brand) if brand else False
    if not brand or brand_low.startswith("авто") or brand_is_utp:
        variants = [
            "Новые авто в кредит. Первый взнос 0 ₽. КАСКО на 1 год. Оставьте заявку.",
            "Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Подберем условия от 15 банков.",
            "Выгода до 45% на новые авто. Трейд-ин выше рынка. Узнайте условия.",
        ]
    else:
        variants = [
            f"Кредит на {brand}. Первый взнос 0 ₽. КАСКО на 1 год. Заявка онлайн.",
            f"{anchor}. Платеж от 9 000 ₽/мес. Одобрение за 30 минут. Узнайте условия.",
            f"{brand} в кредит от 15 банков. Трейд-ин выше рынка. Выберите авто.",
        ]
    # Б/У-сайт: снять хардкод-тексты про НОВЫЕ авто («Новые авто в кредит. Первый взнос 0 ₽…»,
    # «Выгода до 45% на новые авто…») и добрать нейтральным пулом до полного комплекта из 3.
    # На сайтах НОВЫХ авто `_drop_new_car` — no-op, набор и его порядок не меняются.
    variants = _drop_new_car(variants, site_type) + _drop_new_car(list(_NEUTRAL_CREDIT_TEXTS), site_type)
    out: list[str] = []
    seen: set[str] = set()
    for cand in seq + variants:
        line = _finalize_text_line(cand, _RA_TEXT_MAX)
        if not line:
            continue
        # Дедупликация предложений: «Кредит на Первый взнос 0 ₽. Первый взнос 0 ₽…» → skip
        if _has_dup_clause(line):
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= cap:
            break
    if len(out) < cap:
        for cand in variants:
            line = _finalize_text_line(cand, _RA_TEXT_MAX)
            if not line:
                continue
            if _has_dup_clause(line):
                continue
            low = line.lower()
            if low not in seen:
                seen.add(low)
                out.append(line)
            if len(out) >= cap:
                break
    return out[:cap]


def _responsive_ad(titles, texts, href: str, image_hashes=None, display_path: str = "",
                   site_type: str = "") -> dict | None:
    """Собрать ResponsiveAd (Комбинаторное): несколько заголовков + текстов в одном объявлении.
    → dict для ads.add ИЛИ None, если нет обязательных Titles/Texts/Href."""
    # #5/#6 Когерентность скидок в ОДНОМ объявлении (заголовки+тексты): одно ₽/%-значение (эталон —
    # самое частое) → заголовок и текст согласованы, почти-дубли с разными суммами схлопнутся дедупом.
    titles, texts = _coherent_discounts(list(titles or []), list(texts or []))
    titles = _upgrade_credit_titles(list(titles or []), _RA_TITLES_CAP, site_type)
    texts = _upgrade_credit_texts(list(texts or []), _RA_TEXTS_CAP, site_type)
    t = _dedup_cap(_combo_fill_titles(titles), _RA_TITLE_MAX, _RA_TITLES_CAP)
    x = _dedup_cap(_combo_fill_texts(texts), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if len(t) < _RA_TITLES_CAP:
        t = _dedup_cap(t + _combo_fill_titles(t), _RA_TITLE_MAX, _RA_TITLES_CAP)
    if len(x) < _RA_TEXTS_CAP:
        x = _dedup_cap(x + _combo_fill_texts(x), _RA_TEXT_MAX, _RA_TEXTS_CAP)
    if not (t and x and href):
        return None
    ad: dict = {"Titles": t, "Texts": x, "Href": href}
    imgs = [h for h in (image_hashes or []) if h]
    if imgs:
        # v501 ResponsiveAd expects raw JSON array of hashes.
        ad["AdImageHashes"] = imgs[:5]
    if display_path:
        ad["DisplayUrlPath"] = display_path[:20]
    return ad


def _responsive_image_hashes(ra: dict | None) -> list[str]:
    """Return ResponsiveAd image hashes from either v501 payload or legacy raw-list payload."""
    val = (ra or {}).get("AdImageHashes")
    if isinstance(val, dict):
        return [h for h in (val.get("Items") or []) if h]
    if isinstance(val, list):
        return [h for h in val if h]
    return []


def _responsive_retry_items(items: list[dict], *, drop_sitelinks: bool = False,
                            drop_images: bool = False) -> list[dict]:
    out = []
    for it in items or []:
        it2 = {"AdGroupId": it.get("AdGroupId"), "ResponsiveAd": dict(it.get("ResponsiveAd") or {})}
        if drop_sitelinks:
            it2["ResponsiveAd"].pop("SitelinkSetId", None)
        if drop_images:
            it2["ResponsiveAd"].pop("AdImageHashes", None)
        out.append(it2)
    return out
_CALLOUT_POOL_CAP = 200       # макс. уникальных «Уточнений» (AdExtensions) на проход
# Автотаргетинг в v5: спецключ "---autotargeting" в группе (вместо реальных ключей). НЕ ресурс
# relevancematch (его в v5 нет — 404). Проверено live на боевом аккаунте porg-36k7btt7.
_AUTOTARGET_KW = "---autotargeting"


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# Полные формы коротких УТП-хвостов для добивки уточнений до 17-25 символов.
# Упорядочены: сначала длиннее (предпочитаем максимально заполненный вариант).
# Все ≤ _CALLOUT_POOL_TEXT_MAX (25). Правило Семёна: уточнения заполнять ≤8 свободных.
_CALLOUT_EXPAND_POOL = [
    "Гарантия на автомобиль",   # 22
    "Гарантия производителя",   # 22
    "Трейд-ин выше рынка",      # 20
    "Одобрение за 5 минут",     # 20
    "Тест-драйв бесплатно",     # 20
    "Официальная гарантия",     # 20
    "Кредит от 15 банков",      # 20
    "В наличии у дилера",       # 18
    "Выгода при покупке",       # 18
    "Быстрое оформление",       # 18
    "Подбор авто онлайн",       # 18
    "Первый взнос 0 ₽",         # 16
    "КАСКО в подарок",          # 15
    "Авто в наличии",           # 14
]


def _expand_callout(s: str) -> str:
    """Расширить короткое уточнение до более полной формы из пула УТП (≤25 симв).
    Пример: «Трейд-ин» (9) → «Трейд-ин выше рынка» (20). Срабатывает только
    когда len(s) < 17 и найдена запись из _CALLOUT_EXPAND_POOL, начинающаяся с s.
    Если расширение не найдено — возвращает оригинал без изменений."""
    if not s or len(s) >= 17:
        return s
    s_l = s.lower().replace("ё", "е").strip()
    for exp in _CALLOUT_EXPAND_POOL:
        exp_l = exp.lower().replace("ё", "е")
        if exp_l.startswith(s_l) and len(exp) <= _CALLOUT_POOL_TEXT_MAX:
            return exp
    return s


def _normalize_callout_text(text: str) -> str:
    """Нормализовать уточнение до создания ассета Директа."""
    s = str(text or "").strip()
    # В кредитных автосвязках нужен КАСКО; ОСАГО в уточнениях было ошибкой контента.
    s = re.sub(r"(?i)\bосаго\b", "КАСКО", s)
    # Исправление типичных M3-опечаток (LLM иногда роняет букву):
    # «3 латежа» → «3 платежа»  (пропущена «п» в «платеж»)
    s = re.sub(r"(?i)\bлатеж",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «3 платеда» → «3 платежа»  (опечатка «д» вместо «ж»)
    s = re.sub(r"(?i)\bплатед",
               lambda m: ("П" if m.group()[0].isupper() else "п") + "латеж", s)
    # «Рапродаем/рапродаём» → «Распродаем/распродаём»  (пропущена «с» в «распродаж»)
    s = re.sub(r"(?i)\bрапрода",
               lambda m: ("Р" if m.group()[0].isupper() else "р") + "аспрода", s)
    # Расширение через _CALLOUT_EXPAND_POOL отключено: добавляло непроверенные обещания.
    # Уточнения возвращаются как есть из слепка (достоверность > заполнение лимита).
    return s[:_CALLOUT_MAX_EACH].strip()


def _callout_semantic_key(text: str) -> str:
    """Смысловой ключ уточнения: не даём двум УТП одного смысла пройти как разные строки.
    Нормализует ценовые «<кредит/платёж> от N р/мес» (любая сумма → один ключ) и
    «освобождаем склад/склады/стоянку -45%» (стемминг склад*/стоянк* + нормализация
    дефисов/двоеточий «-45% / --45% / -45:» → один ключ). Иначе десятки почти-дублей с разной
    цифрой/окончанием уходят как «разные»."""
    s = str(text or "").lower().replace("ё", "е")
    s = re.sub(r"[-–—]+", "-", s)                    # любые тире (--/–/—) → один дефис
    # «Освобождаем склад/склады/стоянку -45% / --45% / -45:» → один ключ (стемминг + дефис/двоеточие)
    if re.search(r"освобожда", s) and re.search(r"(склад|стоянк|сток)", s):
        return "free_stock"
    if "шин" in s or "резин" in s:
        return "tires"
    if "каско" in s:
        return "kasko"
    # «Платеж/взнос от N р/мес» — уже схлопывались по слову; ценовой «автокредит от N р/мес» — нет.
    if "платеж" in s:
        return "payment"
    if "взнос" in s:
        return "first_payment"
    if "трейд" in s:
        return "tradein"
    if "одобр" in s:
        return "approval"
    # «Автокредит/кредит от N руб/мес» (любая сумма) → один смысловой ключ. НЕ трогаем
    # «кредит от 15 банков» (нет руб/мес → остаётся отдельным офером).
    if (re.search(r"\b(авто)?кредит\b", s) and re.search(r"\bот\b", s)
            and re.search(r"(руб|р\s*/?\s*мес|₽|/\s*мес|в\s*мес)", s)):
        return "credit_monthly"
    return re.sub(r"\s+", " ", s).strip()


# Разумный максимум показываемых уточнений на кампанию: Яндекс выводит ограниченное число,
# десятки почти-дублей бессмысленны. Пул AdExtensions на аккаунт — отдельный кап (_CALLOUT_POOL_CAP).
_CALLOUT_PER_CAMPAIGN_CAP = 8


def _dedup_callouts(texts, cap: int = _CALLOUT_PER_CAMPAIGN_CAP) -> list:
    """Семантический дедуп уточнений + кап. Один смысловой ключ (_callout_semantic_key) → одно
    уточнение; ценовые «от N р/мес» и склад/склады/стоянку схлопываются. Возвращает ≤cap строк
    (нормализованных через _normalize_callout_text). Разные смысловые оферы — сохраняются."""
    seen: set = set()
    out: list = []
    for t in texts or []:
        t = _normalize_callout_text(t)
        if not t:
            continue
        k = _callout_semantic_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= cap:
            break
    return out


def _dedup_callout_ids(co_map: dict, cap: int = 8) -> list:
    """Нормализовать тексты из {text: id}, семантически дедупить, вернуть ≤cap id.
    Предотвращает «ОСАГО» вместо «КАСКО» и два «шины»/«шиномонтаж» в одном наборе (#24)."""
    norm_to_id: dict = {}
    for t, cid in (co_map or {}).items():
        nt = _normalize_callout_text(str(t))
        if nt and nt not in norm_to_id:
            norm_to_id[nt] = cid
    clean_texts = _dedup_callouts(list(norm_to_id), cap=cap)
    return [norm_to_id[t] for t in clean_texts if t in norm_to_id]


_CALLOUT_POOL_V5_CAP = 20        # max AdExtensionId в пуле, возвращаемом v5_ensure_callout_pool
_CALLOUT_POOL_TEXT_MAX = 25      # лимит символов Яндекса на одно уточнение


def v5_ensure_callout_pool(token: str, login: str, texts: list,
                           v5_call_fn, *, cap: int = _CALLOUT_POOL_V5_CAP) -> list:
    """Создать/переиспользовать пул CALLOUT AdExtensions через v5 API.

    Алгоритм:
    1. Нормализует тексты: trim по слову до ≤25 символов; длиннее — отброс.
    2. Дедуп с уже существующими в аккаунте (adextensions.get, case-insensitive).
    3. Недостающие создаёт батчем (adextensions.add); частичные ошибки валидации
       пропускает, не валит весь пул.
    4. Возвращает список AdExtensionId, ≤cap.

    v5_call_fn — callable(svc, method, token, login, params) → dict (blueprint._v5_call).
    Безопасен при отсутствии токена/текстов: возвращает [].
    НЕ зависит от globals-инъекции (configure); пригоден и из repair-скриптов.
    """
    if not token or not texts or not v5_call_fn:
        return []
    # 1. Нормализация + trim по слову до ≤25 символов; дропаем если после trim всё равно >25
    normed: list[str] = []
    seen_lower: set[str] = set()
    for t in texts:
        t = str(t or "").strip()
        if not t:
            continue
        if len(t) > _CALLOUT_POOL_TEXT_MAX:
            try:
                t = _trim_clean(t, _CALLOUT_POOL_TEXT_MAX).strip()
            except Exception:  # noqa: BLE001
                t = t[:_CALLOUT_POOL_TEXT_MAX].strip()
        if not t or len(t) > _CALLOUT_POOL_TEXT_MAX:
            continue
        lk = t.lower()
        if lk in seen_lower:
            continue
        seen_lower.add(lk)
        normed.append(t)
        if len(normed) >= cap * 3:   # достаточно широкий пре-пул до дедупа с существующими
            break
    if not normed:
        return []
    # 2. Читаем существующие уточнения аккаунта (adextensions.get) с пагинацией
    existing: dict[str, int] = {}   # lower_text → AdExtensionId
    try:
        offset = 0
        while True:
            j = v5_call_fn("adextensions", "get", token, login, {
                "SelectionCriteria": {"Types": ["CALLOUT"]},
                "FieldNames": ["Id", "Type"],
                "CalloutFieldNames": ["CalloutText"],
                "Page": {"Limit": 1000, "Offset": offset},
            })
            res = (j.get("result") or {})
            for ext in res.get("AdExtensions", []):
                txt = ((ext.get("Callout") or {}).get("CalloutText") or "").strip()
                if txt:
                    existing[txt.lower()] = int(ext["Id"])
            limited = res.get("LimitedBy")
            if not limited:
                break
            offset = int(limited)
    except Exception:  # noqa: BLE001 — провал чтения не блокирует создание новых
        pass
    # 3. Разбиваем тексты на уже существующие (переиспользуем Id) и новые
    ids: list[int] = []
    to_create: list[str] = []
    for t in normed:
        if t.lower() in existing:
            ids.append(existing[t.lower()])
        else:
            to_create.append(t)
    # 4. Создаём недостающие батчем; частичные ошибки валидации пропускаем
    for i in range(0, len(to_create), 50):
        chunk = to_create[i:i + 50]
        try:
            j = v5_call_fn("adextensions", "add", token, login,
                           {"AdExtensions": [{"Callout": {"CalloutText": t}} for t in chunk]})
            for t, r in zip(chunk, (j.get("result") or {}).get("AddResults", [])):
                if isinstance(r, dict) and r.get("Id"):
                    ids.append(int(r["Id"]))
                    # ошибки в r.get("Errors") — пропускаем битый элемент, не бросаем
        except Exception:  # noqa: BLE001 — одна пачка не валит весь пул
            pass
    return ids[:cap]


def _ensure_callout_exts(token: str, login: str, texts: list) -> dict:
    """Создать «Уточнения» (Callout AdExtensions) для уникальных текстов → {text: ext_id}.
    Дедуп (регистронезависимо), ≤25 симв., кап пула. Упавшие молча пропускаем (callouts необяз.)."""
    clean = _dedup_callouts(texts, cap=_CALLOUT_POOL_CAP)   # единый семантический дедуп + кап пула
    pool: dict = {}
    for chunk in _chunks(clean, 50):
        j = _v5_call("adextensions", "add", token, login,
                     {"AdExtensions": [{"Callout": {"CalloutText": t}} for t in chunk]})
        res = (j.get("result") or {}).get("AddResults", [])
        for t, r in zip(chunk, res):
            if r.get("Id"):
                pool[t] = r["Id"]
        time.sleep(_AC_BATCH_SLEEP)
    return pool

"""Роуты вкладки «Смена изображения» редактора контента Директа.

Отдельный модуль по образцу ``content_price_check_routes`` — ``routes_content_editor``
уже 2900+ строк, раздувать его нечем.

Что делает вкладка: показывает инвентарь картинок аккаунта (РСЯ-объявления + креативы
UAC/МК), даёт загрузить новый файл и заменить им выбранную картинку 1:1 во всех
затронутых объявлениях/кампаниях аккаунта (опционально — только в выбранных кампаниях).

Два транспорта записи, как и во всём редакторе:
  * РСЯ/поиск — cookie/Grid: ``GridClient.upload_image`` → ``update_ad_images``
    для адаптивных (старый хэш меняется на новый В ТОЙ ЖЕ ПОЗИЦИИ ``imageHashes``)
    и ``update_text_ad_images`` для обычных текстовых (``UpdateTextAds``,
    ``textBannerImageHash`` — ОДНА картинка скаляром);
  * UAC (tp6/tp7) — cookie web-api: ``UacClient.upload_image_file`` → PATCH
    ``/uac/campaign/{id}`` со списком ``content_ids`` (позиция сохраняется).

Баллы v5 НЕ тратятся: ``adimages.get`` не вызывается нигде — инвентарь строится из
уже читаемых Grid-полей объявления и из ``contents[]`` детали UAC-кампании.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Callable

from flask import jsonify, request, send_file

# ── лимиты загрузки ──────────────────────────────────────────────────────────
_MAX_FILES = 20
_MAX_BYTES = 40 << 20          # 40 МБ на запрос
_MAX_OUTPUT_BYTES = 10 << 20   # 10 МБ — лимит на файл ПОСЛЕ конвертации в JPEG q80
_TMP_TTL_SEC = 24 * 3600       # временные файлы живут сутки

# Поиск (tp2/tp4) читается НАРАВНЕ с остальными кампаниями: по спеке картинок там быть
# не должно, но фактически они встречаются, и заменять их надо. Отсекать поиск по tp-метке
# нельзя — это давало тихую неполноту («не поддерживается» вместо реально существующих
# картинок). Кампания без картинок просто не даёт карточек, отдельной строки в skipped
# для неё нет. В skipped остаются: объявления ПО ФИДУ (исключение Семёна — картинка приходит
# из фида) и типы, у которых своей картинки нет вовсе (GdDynamicAd, GdPostAd — живой замер
# porg-gcegsszl 2026-07-19: bannerImage непуст у 0 из 95 и 0 из 38 соответственно).
# GdTextAd поддержан отдельной мутацией UpdateTextAds (картинка скаляром textBannerImageHash).

# ЧАСТЬ tp6/tp7 (МК и товарка) видна ОБОИМ транспортам: адаптивные объявления приходят
# в Grid-индексе, а те же картинки лежат в ``contents`` UAC-кампании. Писать в такую
# кампанию двумя транспортами нельзя — владельцем считается UAC. Но принадлежность
# определяется ТОЛЬКО фактом чтения кампании UAC-инвентарём (см. ``_uac_owned_cids``),
# а НЕ tp-меткой имени: tp6/tp7 по имени сильно шире того, что UAC умеет обслужить.


# ───────────────────────────── общие хелперы ────────────────────────────────

def _tp_from_campaign(name: str, ctype: str = "") -> str:
    """Код типа кампании из её имени: ``tp1_cpc_site – …`` → ``tp1``.

    В именах Директа tp-код всегда идёт префиксом (инстинкт из MEMORY.md: читать
    ПРЕФИКС, а не слова «рся»/«поиск»). Не распознали — пустая строка, и кампания
    ни во что не фильтруется (лучше показать, чем молча выбросить).
    """
    # (?!\d) вместо \b: после «tp1_» границы слова НЕТ (цифра и «_» — оба word-символы),
    # поэтому \b здесь не сработал бы ни на одном реальном имени кампании.
    m = re.match(r"^\s*tp(\d)(?!\d)", str(name or ""), re.IGNORECASE)
    if m:
        return f"tp{m.group(1)}"
    if str(ctype or "").upper() in {"UNIFIED_CAMPAIGN", "UAC", "SMART_CAMPAIGN"}:
        return "tp6"
    return ""


def _parse_campaign_ids(raw) -> list[int]:
    """csv-строка или список → отсортированный список положительных id."""
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[int] = []
    for part in parts:
        try:
            cid = int(str(part).strip())
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
    return sorted(out)


def _tmp_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "direct_ce_images")
    os.makedirs(path, exist_ok=True)
    return path


_TMP_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def _tmp_path(tmp_id: str) -> str | None:
    """Путь к временному файлу по tmp_id.

    tmp_id жёстко валидируется по маске hex — иначе строка из тела запроса
    попадёт в join и даст обход каталога (``../../etc/passwd``).
    """
    tmp_id = str(tmp_id or "").strip().lower()
    if not _TMP_ID_RE.match(tmp_id):
        return None
    return os.path.join(_tmp_dir(), f"{tmp_id}.jpg")


def _sweep_tmp() -> None:
    """Подчистить временные картинки старше суток (без внешнего крона)."""
    now = time.time()
    try:
        base = _tmp_dir()
        for name in os.listdir(base):
            path = os.path.join(base, name)
            try:
                if now - os.path.getmtime(path) > _TMP_TTL_SEC:
                    os.unlink(path)
            except OSError:
                continue
    except OSError:
        pass


def _to_jpeg(file_bytes: bytes) -> tuple[bytes, int, int]:
    """Байты картинки → (JPEG q80, width, height).

    Подход 1:1 из ``copy_other._copy_images_upload`` (:315-331): альфа
    композитится на БЕЛЫЙ фон до конвертации, иначе ``convert("RGB")``
    отбрасывает альфа-канал и прозрачный фон становится чёрным квадратом.
    """
    from PIL import Image as _Image
    import io as _io

    img = _Image.open(_io.BytesIO(file_bytes))
    has_alpha = (img.mode in ("RGBA", "LA", "PA")
                 or (img.mode == "P" and "transparency" in img.info))
    if has_alpha:
        img = img.convert("RGBA")
        bg = _Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])   # split()[-1] = alpha
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), int(img.width), int(img.height)


# ───────────────────────────── инвентарь ────────────────────────────────────

# Служебные «имена», не несущие информации: Яндекс подставляет последний сегмент
# URL источника, когда картинку загрузили по ссылке. Живой замер porg-bzti5ud7
# 2026-07-19: Grid ``images{name}`` отдаёт ``'thumb'`` у ВСЕХ 5 картинок аккаунта
# (хвост ``avatars.mds.yandex.net/get-uac/<uuid>/thumb``) — подпись одинаковая у
# всех карточек и различить их можно только глазами. Такое имя хуже короткого
# хэша: хэш хотя бы уникален. Сверяем стем без расширения, чтобы ``thumb`` и
# ``thumb.jpg`` отсекались одинаково.
_JUNK_IMAGE_NAMES = frozenset({
    "thumb", "thumbnail", "preview", "orig", "original", "image", "img",
    "photo", "picture", "banner", "default", "untitled", "unnamed", "noname",
})


def _meaningful_name(raw: object, img_hash: str = "") -> str:
    """Имя картинки, если оно информативно, иначе ``""`` (фронт покажет хэш).

    Пусто / равно хэшу / служебное слово → ``""``. Имя из URL НЕ выдумываем:
    честный короткий хэш (``ceImgLabel`` → класс ``.hashlike``) полезнее
    мусорного слова, одинакового у всех карточек.
    """
    name = str(raw or "").strip()
    if not name or (img_hash and name == str(img_hash)):
        return ""
    stem = name.rsplit(".", 1)[0].strip() if "." in name else name
    return "" if stem.casefold() in _JUNK_IMAGE_NAMES else name


def _image_meta_from_ads(ads_by_id: dict) -> dict[str, dict]:
    """Метаданные картинок (имя/превью/размеры) из уже прочитанных объявлений.

    ``adaptive_ads_for_update`` отдаёт по объявлению богатый ``images``
    (``{imageHash, name, mdsGroupId, width, height, preview_url}``) — отдельный
    Grid-запрос за инвентарём картинок не нужен: те же данные уже приехали
    вместе с RMW-снимком. 0 лишних Grid-запросов.
    """
    out: dict[str, dict] = {}
    for ad in (ads_by_id or {}).values():
        for img in (ad.get("images") or []):
            if not isinstance(img, dict):
                continue
            h = str(img.get("imageHash") or "")
            if not h or h in out:
                continue
            out[h] = {
                "name": _meaningful_name(img.get("name"), h),
                "preview_url": str(img.get("preview_url") or ""),
                "width": int(img.get("width") or 0),
                "height": int(img.get("height") or 0),
            }
    return out


# Полный список кампаний аккаунта ПО КУКЕ. v5 ``campaigns.get`` НЕ отдаёт tp6/tp7
# (UAC/МК/товарка) и tp8 (Telegram) — живой замер porg-gcegsszl 2026-07-19: v5 видит
# 82 кампании, Grid — 157 (неархивных 69), и ВСЕ адаптивные объявления аккаунта лежат
# именно в v5-невидимых кампаниях. Пока список строился из v5, вкладке было доступно
# 0 адаптивных объявлений из 12, и она об этом молчала.
# Публичного «списка всех кампаний» в ``grid_finalize.GridClient`` нет
# (``campaigns_edit_rows`` / ``read_campaign_invariants`` / ``_read_broad_match_map``
# требуют уже готовые id), а ``routes_content_editor._grid_tp67_campaigns`` жёстко
# режет всё, кроме tp6/tp7 (tp8 туда не попадает) — поэтому здесь минимальный
# собственный селект. Правильное место для него — метод GridClient (см. отчёт).
_GRID_CAMPAIGNS_Q = (
    "query ImagesTabCampaigns($login:String!,$inp:GdCampaignsContainerInput!){"
    "client(searchBy:{login:$login}){campaigns(input:$inp){rowset{id name "
    "__typename status{archived}}}}}")

# Лёгкий индекс объявлений: только id/кампания/тип. Нужен, чтобы (а) находить
# объявления в кампаниях, невидимых v5 (v5 ``ads.get`` по ним ничего не отдаёт),
# (б) заранее знать типы и честно отчитаться о том, что вкладка не умеет менять.
_GRID_ADS_INDEX_Q = (
    "query ImagesTabAdsIndex($login:String!,$inp:GdAdsContainerInput!){"
    "client(searchBy:{login:$login}){ads(input:$inp){rowset{id campaignId "
    "__typename}}}}")

_GRID_CAMPAIGNS_PAGE = 200

# Объявления, картинка которых приходит ИЗ ФИДА, — исключение Семёна: вкладка их не трогает.
# Живой read-only замер 2026-07-19: у ``GdShoppingAd`` (844) и ``GdListingAd`` (936) на
# porg-gcegsszl поле ``feed{id}`` непусто у 100%; ``GdSmartAd`` (24, type PERFORMANCE_MAIN) —
# смарт-баннер над фидом; ``GdMlAutoSuggestAd`` (4 на porg-bzti5ud7) тоже фидовый и картинку
# генерирует сам (``feed{id}=3403773``, ``imageGenerationTypes=[GENERATED_IMAGE, SITE_IMAGE]``,
# ``bannerImage=null``) — заменять там нечего.
_FEED_AD_TYPES = frozenset({
    "GdShoppingAd", "GdListingAd", "GdSmartAd", "GdMlAutoSuggestAd",
})


def _grid_campaign_archived(row: dict) -> bool:
    """True, если Grid явно пометил кампанию архивной."""
    status = row.get("status") if isinstance(row, dict) else None
    if isinstance(status, dict):
        return bool(status.get("archived"))
    return False


def _grid_campaigns(grid, login: str) -> list[dict]:
    """Все кампании аккаунта через Grid → сырые строки rowset.

    В запросе видны и архивные, но write-path редактора контента их намеренно
    отфильтровывает ниже: архивные объявления не изменяем.
    """
    out: list[dict] = []
    offset = 0
    for _ in range(200):        # предохранитель от бесконечной пагинации
        inp = {
            "filter": {},
            "statRequirements": {"preset": "LAST_30DAYS", "goalIds": [],
                                 "useCampaignGoalIds": True},
            "limitOffset": {"limit": _GRID_CAMPAIGNS_PAGE, "offset": offset},
            # Сортировка по ID, а не по STATUS: при offset-пагинации ключ обязан быть
            # уникальным, иначе строки с равным статусом могут переставляться между
            # страницами (часть кампаний пропадёт, часть придёт дважды). Тот же выбор,
            # что у соседнего ``_ads_rows_paginated``.
            "orderBy": [{"order": "ASC", "field": "ID"}],
        }
        # Список кампаний — ЕДИНСТВЕННЫЙ вход всей вкладки (v5 больше не подстраховывает),
        # поэтому через ту же обёртку, что и чтение объявлений: 3 попытки с backoff на
        # транзиенты Яндекса + внятная GridFinalizeError на не-JSON вместо голого .json().
        data = grid._post_json_retry("ImagesTabCampaigns", _GRID_CAMPAIGNS_Q,
                                     {"login": login, "inp": inp})
        rows = ((((data.get("data") or {}).get("client") or {})
                 .get("campaigns") or {}).get("rowset") or [])
        out.extend(rows)
        if len(rows) < _GRID_CAMPAIGNS_PAGE:
            break
        offset += len(rows)
    return out


def _grid_ads_index(grid, campaign_ids: list[int]) -> list[dict]:
    """id/campaignId/__typename всех объявлений кампаний — через ту же пагинацию.

    ``_ads_rows_paginated`` уже умеет постраничное чтение (фикс «limit 5000 молча
    терял объявления») — переиспользуем его, а не пишем свой цикл.
    """
    rows: list[dict] = []
    for chunk in [campaign_ids[i:i + 100] for i in range(0, len(campaign_ids), 100)]:
        rows.extend(grid._ads_rows_paginated("ImagesTabAdsIndex", _GRID_ADS_INDEX_Q, chunk))
    return rows


def _rsya_inventory(token: str, login: str, v5_call: Callable,
                    campaign_ids: list[int], *,
                    grid_client_factory: Callable | None = None) -> tuple[dict, dict, dict, list[dict]]:
    """Инвентарь картинок РСЯ-объявлений.

    → (images_by_hash, ads_by_id, ad_cid, skipped). ``ads_by_id`` — полный
    RMW-снимок объявления из ``adaptive_ads_for_update`` (нужен и превью, и
    записи), ``ad_cid`` — ad_id → campaign_id.

    Источник и списка кампаний, и списка объявлений — Grid (кука, 0 баллов v5).
    v5 ``campaigns.get`` остаётся только КРОСС-ЧЕКОМ полноты: кампании, которые
    он видит, а Grid не отдал, попадают в ``skipped``, а не теряются молча.
    """
    from . import routes_content_editor as rce

    grid = (grid_client_factory or rce._grid_client)(login)
    grid._bootstrap_csrf()

    skipped: list[dict] = []
    camp: dict[int, dict] = {}
    archived_cids: set[int] = set()
    try:
        grid_campaign_rows = _grid_campaigns(grid, login)
    except Exception as exc:  # noqa: BLE001
        logging.warning("images inventory: grid campaign list failed for %s: %r", login, exc)
        skipped.append({"reason": f"Grid не отдал список кампаний ({exc!r}) — инвентарь недоступен",
                        "count": 0, "failed_source": "grid_campaigns"})
        return {}, {}, {}, skipped
    for row in grid_campaign_rows:
        try:
            cid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if cid <= 0 or (campaign_ids and cid not in campaign_ids):
            continue
        if _grid_campaign_archived(row):
            archived_cids.add(cid)
            continue
        name = row.get("name") or ""
        # ctype НЕ передаём: Grid отдаёт свой ``__typename`` (``GdUnifiedCampaign``),
        # а фолбэк ``_tp_from_campaign`` сверяется с v5-значениями (UNIFIED_CAMPAIGN/
        # UAC/SMART_CAMPAIGN) — совпасть они не могут. Маппить нельзя: под
        # ``GdUnifiedCampaign`` у Директа лежат и ЕПК-РСЯ (tp1), и МК (tp6), т.е.
        # typename тип кампании не определяет. Тип берём только из имени.
        camp[cid] = {"id": str(cid), "name": name, "tp": _tp_from_campaign(name)}

    if archived_cids:
        skipped.append({"reason": "архивные кампании/объявления не изменяются",
                        "count": len(archived_cids)})

    # Кросс-чек полноты: v5 не должен видеть кампаний, которых нет в Grid-списке.
    camps, err = rce._v5_paginate(
        v5_call, "campaigns", token, login,
        rce._strip_campaign_subfield_names(
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"]}
        ),
        "Campaigns",
    )
    if err:
        skipped.append({"reason": f"кросс-чек v5 campaigns.get не выполнен: {str(err)[:120]}",
                        "count": 0})
    else:
        v5_only = 0
        for c in camps:
            cid = int(c.get("Id") or 0)
            if not cid or (campaign_ids and cid not in campaign_ids):
                continue
            if cid not in camp:
                v5_only += 1
        if v5_only:
            skipped.append({"reason": "кампании видны v5, но не пришли из Grid — "
                                      "их объявления не прочитаны", "count": v5_only})

    # Читаем ВСЕ кампании аккаунта, включая поисковые tp2/tp4: если у объявления есть
    # непустой imageHashes — картинка обязана попасть в инвентарь и быть заменяемой.
    work_cids = list(camp)
    if not work_cids:
        return {}, {}, {}, skipped

    ad_cid: dict[int, int] = {}
    text_cid: dict[int, int] = {}
    feed_types: dict[str, int] = {}
    other_types: dict[str, int] = {}
    try:
        grid_ads_rows = _grid_ads_index(grid, work_cids)
    except Exception as exc:  # noqa: BLE001
        logging.warning("images inventory: grid ads index failed for %s: %r", login, exc)
        skipped.append({"reason": f"Grid не отдал список объявлений ({exc!r}) — инвентарь недоступен",
                        "count": 0, "failed_source": "grid_ads"})
        return {}, {}, {}, skipped
    for row in grid_ads_rows:
        try:
            aid = int(row.get("id"))
            cid = int(row.get("campaignId"))
        except (TypeError, ValueError):
            continue
        if aid <= 0 or cid not in camp:
            continue
        typename = str(row.get("__typename") or "?")
        if typename == "GdAdaptiveTextAd":
            ad_cid[aid] = cid
        elif typename == "GdTextAd":
            text_cid[aid] = cid
        elif typename in _FEED_AD_TYPES:
            feed_types[typename] = feed_types.get(typename, 0) + 1
        else:
            other_types[typename] = other_types.get(typename, 0) + 1
    # «Сколько именно мы НЕ трогаем» должно быть видно, а не выясняться постфактум —
    # но админу это одна практическая причина («заменить нечем»), а не two технических
    # категории по внутренним именам типов Директа. Технический breakdown (typename→count)
    # остаётся только в логах для расследования, на экран не идёт.
    if feed_types:
        logging.info("images inventory: skipped feed-driven ads %s", feed_types)
    if other_types:
        logging.info("images inventory: skipped ads without own picture %s", other_types)
    skip_n = sum(feed_types.values()) + sum(other_types.values())
    if skip_n:
        skipped.append({"reason": "нет своей картинки для замены (товарные объявления "
                                  "берут картинку из фида, у части типов объявлений своей "
                                  "картинки нет вовсе)",
                        "count": skip_n})
    if not ad_cid and not text_cid:
        return {}, {}, {}, skipped

    ads_by_id: dict[int, dict] = {}
    if ad_cid:
        for aid, item in (grid.adaptive_ads_for_update(
                sorted(set(ad_cid.values())), list(ad_cid)) or {}).items():
            item["kind"] = "adaptive"
            ads_by_id[int(aid)] = item
    if text_cid:
        # GdTextAd — отдельная мутация (UpdateTextAds, картинка скаляром), но форма
        # снимка та же, поэтому дальше инвентарь ходит по обоим одним кодом.
        for aid, item in (grid.text_ads_for_update(
                sorted(set(text_cid.values())), list(text_cid)) or {}).items():
            ads_by_id[int(aid)] = item
    ad_cid = {**ad_cid, **text_cid}

    images: dict[str, dict] = {}
    unsafe_text = 0
    for aid, item in ads_by_id.items():
        cid = ad_cid.get(int(aid))
        if not cid:
            continue
        if item.get("rmw_unsafe"):
            # объявление с полем, чью write-форму подтвердить нечем: в инвентарь его
            # картинку не тянем — иначе карточка обещала бы замену, которой не будет.
            # Считаем ТОЛЬКО те, у которых картинка вообще есть: объявлению без неё
            # замена и не грозила, а в skipped оно завышало цифру.
            if item.get("imageHashes"):
                unsafe_text += 1
            continue
        for h in (item.get("imageHashes") or []):
            h = str(h)
            if not h:
                continue
            entry = images.setdefault(h, {
                "key": h, "hash": h, "name": "", "preview_url": "",
                "width": 0, "height": 0,
                "source": "text" if item.get("kind") == "text" else "rsya",
                "usages": {"ads": 0, "campaigns": []},
                "supported": True, "reason": "",
            })
            entry["usages"]["ads"] += 1
            row = next((c for c in entry["usages"]["campaigns"] if c["id"] == str(cid)), None)
            if row is None:
                row = {**camp[cid], "ads": 0}
                entry["usages"]["campaigns"].append(row)
            row["ads"] += 1

    if unsafe_text:
        skipped.append({"reason": "текстовые объявления с турболендингом/мультикарточками — "
                                  "их состояние нельзя вернуть при полной перезаписи, "
                                  "картинка не заменяется",
                        "count": unsafe_text})
    if images:
        meta = _image_meta_from_ads(ads_by_id)
        for h, m in meta.items():
            if h in images:
                images[h].update({k: v for k, v in m.items() if v})
    return images, {int(k): v for k, v in ads_by_id.items()}, ad_cid, skipped


def _uac_inventory(login: str, campaign_ids: list[int], *,
                   uac_read_client_factory: Callable | None = None) -> tuple[dict, dict]:
    """Инвентарь креативов UAC (tp6/tp7) → (images_by_key, contents_by_campaign).

    ``contents[]`` детали кампании уже содержит и миниатюру (``thumb``), и
    ``direct_image_hash`` — отдельный запрос за картинками (и баллы) не нужен.
    """
    from . import routes_content_editor as rce

    reader = rce._uac_read_client(login, uac_read_client_factory)
    cids: list[int] = []
    for row in reader.client.list_campaigns():
        if not isinstance(row, dict):
            continue
        try:
            cid = int(row.get("id") or row.get("direct_id") or row.get("campaign_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid > 0 and (not campaign_ids or cid in campaign_ids):
            cids.append(cid)
    if not cids:
        return {}, {}

    details = reader.campaign_details(cids) or {}
    images: dict[str, dict] = {}
    contents_by_campaign: dict[int, list[dict]] = {}
    for raw_cid, raw in details.items():
        cid = int(raw_cid)
        detail = rce._unwrap_uac_response(raw) if not isinstance(raw, dict) else raw
        contents = [c for c in (detail.get("contents") or []) if isinstance(c, dict)]
        contents_by_campaign[cid] = contents
        name = (detail.get("display_name") or detail.get("name") or f"UAC {cid}")
        camp = {"id": str(cid), "name": name, "tp": _tp_from_campaign(name, "UAC")}
        for c in contents:
            content_id = str(c.get("id") or "")
            if not content_id:
                continue
            img_hash = str(c.get("direct_image_hash") or "")
            # Ключ сущности — imageHash (контент-адресный: одинаковые картинки
            # схлопываются сами, в т.ч. между РСЯ и UAC). Хэша нет (видео/
            # нестандартный креатив) — ключуем по id креатива.
            key = img_hash or f"uac:{content_id}"
            entry = images.setdefault(key, {
                "key": key, "hash": img_hash,
                "name": _meaningful_name(c.get("filename"), img_hash),
                "preview_url": str(c.get("thumb") or ""),
                "width": int(c.get("ow") or c.get("iw") or 0),
                "height": int(c.get("oh") or c.get("ih") or 0),
                "source": "uac",
                "usages": {"ads": 0, "campaigns": []},
                "supported": True, "reason": "",
            })
            entry["usages"]["ads"] += 1
            row = next((x for x in entry["usages"]["campaigns"] if x["id"] == str(cid)), None)
            if row is None:
                row = {**camp, "ads": 0}
                entry["usages"]["campaigns"].append(row)
            row["ads"] += 1
    return images, contents_by_campaign


def _merge_inventory(rsya: dict, uac: dict) -> list[dict]:
    """Слить два инвентаря по ключу; одинаковый хэш в РСЯ и UAC = одна карточка."""
    out: dict[str, dict] = {}
    for src in (rsya, uac):
        for key, entry in src.items():
            cur = out.get(key)
            if cur is None:
                out[key] = json.loads(json.dumps(entry))
                continue
            cur["usages"]["ads"] += entry["usages"]["ads"]
            for camp in entry["usages"]["campaigns"]:
                row = next((c for c in cur["usages"]["campaigns"] if c["id"] == camp["id"]), None)
                if row is None:
                    cur["usages"]["campaigns"].append(dict(camp))
                else:
                    row["ads"] = row.get("ads", 0) + camp.get("ads", 0)
            # Пустое имя добираем из второго транспорта — это и есть спасение
            # подписи: Grid отдаёт служебный ``thumb`` (обнулён `_meaningful_name`),
            # а UAC по тому же хэшу знает настоящий ``filename``
            # («Chery Tiggo 4 pro.jpg»). ``source`` при этом НЕ меняем: транспорт
            # определяется фактом чтения, а не тем, откуда пришла подпись.
            for field in ("name", "preview_url"):
                if not cur.get(field) and entry.get(field):
                    cur[field] = entry[field]
            for field in ("width", "height"):
                if not cur.get(field) and entry.get(field):
                    cur[field] = entry[field]
    return sorted(out.values(), key=lambda e: (-e["usages"]["ads"], e["key"]))


# ───────────────────── разделение транспортов (tp6/tp7) ─────────────────────
# Живой read-only probe porg-gcegsszl 2026-07-19 (мутаций 0): у 35 UAC-кампаний
# аккаунта в Grid нашлось 12 адаптивных объявлений в 11 кампаниях, и НИ ОДНОГО
# хэша, которого не было бы в ``contents`` этой же кампании (grid_only = 0 по всем
# 34 сравнённым кампаниям; там где адаптивы есть — совпадение 5/5). Остальные 23
# UAC-кампании адаптивных объявлений в Grid не имеют вовсе. Т.е. картинки МК/товарки
# живут в ``contents`` кампании, а адаптивное объявление — их проекция.
#
# Отсюда выбор: для кампании, которой владеет UAC, пишем ТОЛЬКО UAC-легом. Grid-лег
# для неё не добавляет покрытия, зато давал вторую запись в ту же кампанию (full-PATCH
# c REPLACE-семантикой поверх Grid-мутации) и второй аплоад того же файла во вторую
# библиотеку. Что проекция реально обновилась — не постулируем, а ПРОВЕРЯЕМ
# (``_verify_uac_mirror``): расхождение попадает в ошибки задания, а не замалчивается.

def _uac_owned_cids(uac_contents: dict) -> set[int]:
    """Кампании, картинками которых владеет UAC → Grid-лег их не трогает.

    Единственный источник — кампании, РЕАЛЬНО прочитанные UAC-инвентарём, и только
    те, у кого ``contents`` непусты: владение = «UAC может эту кампанию обслужить»,
    а не «имя похоже на tp6/tp7».

    Почему НЕ «∪ tp6/tp7 по имени» (было до 2026-07-19, регресс): замер на
    `porg-gcegsszl` показал, что UAC ``list_campaigns`` — ПОДМНОЖЕСТВО tp6/tp7-по-имени
    (35 UAC ⊂ 63 tp6/tp7; 28 кампаний — архивные МК и tp7-товарка — UAC-транспортом
    не пишутся вовсе). Объединение не страховало падение инвентаря, а систематически
    запрещало Grid-лег там, где UAC заменить НЕ МОЖЕТ: 12 хэшей оставались без единого
    транспорта, включая живьём подтверждённый Grid-путь кампании 704589546.

    Падение UAC-инвентаря запрет НЕ расширяет: ``uac_contents`` пуст → ``uac_owned``
    пуст → Grid-лег работает как раньше (двойной записи при этом быть не может —
    UAC-лег без инвентаря карту замены не строит), а сама ошибка чтения уже лежит
    в ``errors`` задания (`run_image_replace`, ветка «инвентарь UAC»).
    """
    out: set[int] = set()
    for cid, contents in (uac_contents or {}).items():
        if not contents:
            # Кампания без креативов: заменить в ней UAC нечего, а запрет Grid-лега
            # оставил бы её объявления без транспорта вовсе.
            continue
        try:
            out.add(int(cid))
        except (TypeError, ValueError):
            continue
    return out


def _grid_transport_scan(ads_by_id: dict, ad_cid: dict, img_hash: str,
                         uac_owned: set[int]) -> dict:
    """Один проход по объявлениям: где Grid РЕАЛЬНО может заменить ``img_hash``.

    → ``{"cids": {…}, "blocked_uac": n, "blocked_unsafe": n}``.

    Запрет Grid-лега на UAC-владеемых кампаниях действует ТОЛЬКО на АДАПТИВНЫЕ
    объявления — и только для них он доказан. Живой read-only замер `porg-gcegsszl`
    2026-07-19 (мутаций 0, per-campaign сверка Grid-хэшей с ``contents``):

      * адаптивные в UAC-владеемых кампаниях: ``grid_only = 0`` по всем 11 кампаниям
        → адаптив действительно ПРОЕКЦИЯ ``contents``, второй записи не нужно;
      * ``GdTextAd`` в тех же кампаниях: ``grid_only = 34 хэша в 6 кампаниях``
        → текстовое объявление несёт картинки, которых в ``contents`` НЕТ ВОВСЕ,
        т.е. проекцией оно НЕ является и UAC-PATCH ``content_ids`` до него не доходит.

    Отсюда: 2952 ``GdTextAd`` в UAC-владеемых кампаниях пишет Grid-лег своей мутацией
    (``UpdateTextAds`` — отдельный объект, не тот, что PATCH'ит UAC). Иначе 23 кампании
    (из 34 владеемых) вообще не имели бы транспорта: адаптивных объявлений у них нет,
    а `_verify_uac_mirror` их даже не считал бы в ``checked`` — молчаливая неполнота.
    """
    out: dict = {"cids": set(), "blocked_uac": 0, "blocked_unsafe": 0}
    img_hash = str(img_hash or "")
    if not img_hash:
        return out
    for aid, ad in (ads_by_id or {}).items():
        if img_hash not in {str(h) for h in (ad.get("imageHashes") or [])}:
            continue
        try:
            cid = int((ad_cid or {}).get(int(aid)) or 0)
        except (TypeError, ValueError):
            continue
        if not cid:
            continue
        if ad.get("rmw_unsafe"):
            out["blocked_unsafe"] += 1
            continue
        if ad.get("kind") != "text" and cid in uac_owned:
            out["blocked_uac"] += 1
            continue
        out["cids"].add(cid)
    return out


def _annotate_transport(merged: list[dict], ads_by_id: dict, ad_cid: dict,
                        uac_images: dict, uac_owned: set[int]) -> list[dict]:
    """Проставить ``supported``/``reason`` по РЕАЛЬНО доступному транспорту записи.

    Раньше транспорт считался только в момент исполнения задания (`run_image_replace`),
    и карточка без транспорта показывалась в инвентаре как ``supported: True``: админ
    выбирал её, ждал задание и получал ошибку. Всё, что для этого решения нужно, известно
    уже на этапе сборки инвентаря — считаем здесь.
    """
    for entry in (merged or []):
        key = str(entry.get("key") or "")
        if not entry.get("supported", True):
            continue
        if key in (uac_images or {}):
            continue                       # заменит UAC-лег
        scan = _grid_transport_scan(ads_by_id, ad_cid, key, uac_owned)
        if scan["cids"]:
            continue                       # заменит Grid-лег
        if scan["blocked_unsafe"] and not scan["blocked_uac"]:
            reason = ("объявления с турболендингом/мультикарточками — их состояние "
                      "нельзя вернуть при полной перезаписи, замена не выполняется")
        else:
            reason = ("картинка есть только в адаптивных объявлениях кампаний tp6/tp7 "
                      "(владелец — UAC), но в contents кампании её нет — "
                      "заменить нечем ни одним транспортом")
        entry["supported"] = False
        entry["reason"] = reason
    return merged


def _verify_uac_mirror(login: str, old_hashes_by_cid: dict[int, set[str]],
                       *, grid_client_factory: Callable | None = None) -> dict:
    """Убедиться, что адаптивы UAC-кампаний больше НЕ несут заменённый хэш.

    Проверка намеренно смотрит ТОЛЬКО на ``GdAdaptiveTextAd``: она подтверждает ровно
    одно допущение — «адаптив МК есть проекция ``contents``, поэтому Grid-лег в эту
    кампанию не пишет». Для ``GdTextAd`` это допущение НЕ действует (живой замер
    porg-gcegsszl 2026-07-19: ``grid_only=34`` хэша в 6 кампаниях против ``0`` у
    адаптивных), поэтому текстовые объявления пишет Grid-лег своей мутацией, а не
    проверяет этот метод — см. ``_grid_transport_scan``.

    Кампания без адаптивных объявлений проверять нечего, но молчать об этом нельзя:
    ``no_adaptive`` показывает, сколько кампаний прошло мимо проверки, чтобы
    ``checked: 0`` не читалось как «всё чисто».
    """
    from . import routes_content_editor as rce

    cids = sorted({int(c) for c, hs in (old_hashes_by_cid or {}).items() if hs})
    out: dict = {"checked": 0, "ok": 0, "stale": [], "errors": [],
                 "campaigns": len(cids), "no_adaptive": len(cids)}
    if not cids:
        return out
    try:
        grid = (grid_client_factory or rce._grid_client)(login)
        grid._bootstrap_csrf()
        ad_cid: dict[int, int] = {}
        for row in _grid_ads_index(grid, cids):
            if str(row.get("__typename") or "") != "GdAdaptiveTextAd":
                continue
            try:
                ad_cid[int(row.get("id"))] = int(row.get("campaignId"))
            except (TypeError, ValueError):
                continue
        if not ad_cid:
            return out
        ads = grid.adaptive_ads_for_update(sorted(set(ad_cid.values())), list(ad_cid)) or {}
        live: dict[int, set[str]] = {}
        for aid, item in ads.items():
            cid = ad_cid.get(int(aid))
            if not cid:
                continue
            for h in (item.get("imageHashes") or []):
                if h:
                    live.setdefault(cid, set()).add(str(h))
        out["no_adaptive"] = len(cids) - len(live)
        for cid, hashes in live.items():
            out["checked"] += 1
            still = sorted(hashes & set(old_hashes_by_cid.get(cid) or ()))
            if still:
                out["stale"].append({"campaign_id": str(cid), "hashes": still})
            else:
                out["ok"] += 1
    except Exception as e:  # noqa: BLE001 — проверка не должна съедать результат замены
        out["errors"].append(f"проверка адаптивов МК не выполнена: {str(e)[:180]}")
    return out


# ───────────────────────────── исполнение замены ─────────────────────────────

def _grid_update_reasons(grid) -> list[str]:
    """Причины неполной записи последнего Grid-батча (``GridClient.last_ad_update_errors``).

    Мутации отдают ЧИСЛО, а причина отказа (``validationResult.errors``, напр.
    ``ACTION_IN_ARCHIVED_CAMPAIGN``) лежит на клиенте — без неё задание рапортовало
    успех при нуле изменённых объявлений. ``getattr`` — чтобы старые/тестовые
    grid-объекты без атрибута продолжали работать.
    """
    return [str(x) for x in (getattr(grid, "last_ad_update_errors", None) or []) if x]


_GRID_ARCHIVED_AD_MARKER = "CANNOT_UPDATE_ARCHIVED_AD"


def _grid_reason_is_archived_ad(reason: str) -> bool:
    """True, если одна строка shortfall содержит только ad-level archive причины.

    Старые/тестовые grid-клиенты могли склеить несколько validation errors в одну строку через
    ``;``. Нельзя классифицировать весь батч как архивный, если рядом с
    ``CANNOT_UPDATE_ARCHIVED_AD`` есть любой другой код отказа.
    """
    text = str(reason or "").strip()
    if not text:
        return False
    if " — " in text:
        text = text.split(" — ", 1)[1]
    parts = [p.strip() for p in text.split(";") if p.strip()]
    meaningful = [p for p in parts if not p.startswith("failed_ad_ids=")]
    return bool(meaningful) and all(_GRID_ARCHIVED_AD_MARKER in p for p in meaningful)


def _grid_reasons_are_archived_ads(reasons: list[str]) -> bool:
    """True, если shortfall Grid объяснён только архивными объявлениями."""
    clean = [str(r) for r in (reasons or []) if str(r)]
    return bool(clean) and all(_grid_reason_is_archived_ad(r) for r in clean)


def _replace_rsya_images(login: str, pairs_hashes: dict[str, str],
                         ads_by_id: dict, ad_cid: dict[int, int],
                         *, skip_cids: set[int] | None = None,
                         grid_client_factory: Callable | None = None) -> dict:
    """Заменить старые хэши на новые ВО ВСЕХ затронутых объявлениях РСЯ.

    ``pairs_hashes``: {старый хэш: новый хэш}. Позиция в ``imageHashes``
    сохраняется — полная замена списка Grid'ом иначе перетасует картинки.

    ``skip_cids`` — кампании, которые пишет UAC-лег (tp6/tp7). Их объявления сюда
    не попадают: две записи в одну кампанию двумя транспортами запрещены.
    Сколько объявлений отдано UAC-легу — видно в ``ads_left_to_uac``.
    """
    from . import routes_content_editor as rce

    skip_cids = {int(c) for c in (skip_cids or set())}
    items: list[dict] = []
    text_items: list[dict] = []
    touched_campaigns: set[int] = set()
    left_to_uac = 0
    left_to_uac_cids: set[int] = set()
    skipped_unsafe = 0
    for aid, ad in (ads_by_id or {}).items():
        hashes = list(ad.get("imageHashes") or [])
        if not any(h in pairs_hashes for h in hashes):
            continue
        cid = ad_cid.get(int(aid))
        # UAC-владение отдаёт UAC-легу только АДАПТИВНЫЕ объявления: живой замер
        # (см. _grid_transport_scan) показал grid_only=0 у адаптивных (проекция
        # contents) и grid_only=34 у GdTextAd (НЕ проекция — UAC-PATCH до них не
        # доходит). Текстовые пишем Grid'ом всегда, иначе 23 кампании остались бы
        # без транспорта вовсе. Объект разный, двойной записи в один объект нет.
        if cid and int(cid) in skip_cids and ad.get("kind") != "text":
            left_to_uac += 1
            left_to_uac_cids.add(int(cid))
            continue
        if ad.get("rmw_unsafe"):
            skipped_unsafe += 1
            continue
        new_hashes = [pairs_hashes.get(h, h) for h in hashes]   # позиции 1:1
        item = dict(ad)
        item["imageHashes"] = new_hashes
        # у GdTextAd картинка ОДНА и пишется скаляром textBannerImageHash — список из
        # одного элемента здесь только ради общей формы снимка (см. text_ads_for_update)
        (text_items if ad.get("kind") == "text" else items).append(item)
        if cid:
            touched_campaigns.add(cid)
    errors: list[str] = []
    archived_ads = 0
    if skipped_unsafe:
        errors.append(f"{skipped_unsafe} текстовых объявл. пропущено: турболендинг/"
                      f"мультикарточки нельзя вернуть при полной перезаписи")
    if not items and not text_items:
        return {"replaced": 0, "errors": errors, "campaigns_touched": 0,
                "ads_left_to_uac": left_to_uac,
                "left_to_uac_cids": sorted(left_to_uac_cids)}
    grid = (grid_client_factory or rce._grid_client)(login)
    updated = 0
    if items:
        upd_adaptive = int(grid.update_ad_images(items) or 0)
        why = _grid_update_reasons(grid)
        if upd_adaptive < len(items):
            if _grid_reasons_are_archived_ads(why):
                archived_ads += len(items) - upd_adaptive
            else:
                errors.append(f"Grid обновил {upd_adaptive} адаптивных объявл. из {len(items)}"
                              + (f" — {'; '.join(why)}" if why else ""))
        elif why:
            errors.append(f"Grid (адаптивные): {'; '.join(why)}")
        updated += upd_adaptive
    if text_items:
        upd_text = int(grid.update_text_ad_images(text_items) or 0)
        why = _grid_update_reasons(grid)
        if upd_text < len(text_items):
            if _grid_reasons_are_archived_ads(why):
                archived_ads += len(text_items) - upd_text
            else:
                errors.append(f"Grid обновил {upd_text} текстовых объявл. из {len(text_items)}"
                              + (f" — {'; '.join(why)}" if why else ""))
        elif why:
            errors.append(f"Grid (текстовые): {'; '.join(why)}")
        updated += upd_text
    return {"replaced": int(updated), "errors": errors,
            "campaigns_touched": len(touched_campaigns),
            "ads_adaptive": len(items), "ads_text": len(text_items),
            "ads_archived": int(archived_ads),
            "ads_left_to_uac": left_to_uac,
            "left_to_uac_cids": sorted(left_to_uac_cids)}


def _replace_uac_images(login: str, pairs_content: dict[int, dict[str, str]],
                        *, uac_client_factory: Callable | None = None) -> dict:
    """Заменить креативы UAC-кампаний: {cid: {старый content_id: новый}}.

    Пишем через ``_uac_patch_campaign_texts(..., "content_ids", …)`` —
    full-payload билдер уже деривирует ``content_ids`` из ``contents``, а
    переданные values ставятся ПОСЛЕ него и не затираются.
    """
    from . import routes_content_editor as rce

    if not pairs_content:
        return {"replaced": 0, "errors": [], "campaigns_touched": 0}
    client = rce._uac_client(login, uac_client_factory)
    replaced = 0
    errors: list[str] = []
    touched_ids: list[str] = []
    for cid, mapping in pairs_content.items():
        try:
            detail = rce._unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            current = [str((c or {}).get("id") or "")
                       for c in (detail.get("contents") or []) if isinstance(c, dict)]
            current = [c for c in current if c]
            if not any(c in mapping for c in current):
                # Раньше здесь был молчаливый ``continue``: карта замены строится ИЗ
                # этих же ``contents``, поэтому непопадание значит, что кампания
                # изменилась между инвентарём и записью. Замена не сделана — это
                # обязано быть видно в результате, а не выглядеть успехом.
                errors.append(f"кампания {cid}: старый креатив уже не в кампании "
                              f"(изменилась между чтением и записью) — замена не выполнена")
                continue
            new_ids = [mapping.get(c, c) for c in current]      # позиция сохраняется
            changed = sum(1 for a, b in zip(current, new_ids) if a != b)
            rce._uac_patch_campaign_texts(client, int(cid), "content_ids", new_ids)
            # read-back: новый креатив реально в кампании, старого нет
            after = rce._unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-readback:{cid}"))
            after_ids = [str((c or {}).get("id") or "")
                         for c in (after.get("contents") or []) if isinstance(c, dict)]
            if not all(v in after_ids for v in mapping.values()):
                errors.append(f"кампания {cid}: read-back не подтвердил новую картинку")
                continue
            replaced += changed
            touched_ids.append(str(cid))
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors,
            "campaigns_touched": len(touched_ids), "touched_ids": touched_ids}


def run_image_replace(token: str, login: str, payload: dict, v5_call: Callable,
                      *, grid_client_factory: Callable | None = None,
                      uac_client_factory: Callable | None = None,
                      uac_read_client_factory: Callable | None = None) -> dict:
    """Исполнить задание ``image_replace`` (вызывается из ``_do_replace``).

    payload: ``{"campaign_ids": [...], "pairs": [{"old_key", "tmp_id",
    "tmp_path", "filename"}]}``. Возвращает реальное число изменённых объектов
    и список ошибок (инвариант CONTENT_EDITOR.md).
    """
    from . import routes_content_editor as rce

    campaign_ids = _parse_campaign_ids(payload.get("campaign_ids"))
    pairs = [p for p in (payload.get("pairs") or []) if isinstance(p, dict)]
    if not pairs:
        return {"replaced": 0, "errors": ["не задана ни одна пара замены"]}

    errors: list[str] = []
    # 1) Инвентарь — чтобы знать, где лежит каждый старый ключ.
    rsya_images, ads_by_id, ad_cid, skipped = {}, {}, {}, []
    try:
        rsya_images, ads_by_id, ad_cid, skipped = _rsya_inventory(
            token, login, v5_call, campaign_ids,
            grid_client_factory=grid_client_factory)
    except Exception as e:  # noqa: BLE001
        errors.append(f"инвентарь РСЯ: {str(e)[:180]}")
    uac_images, uac_contents = {}, {}
    try:
        uac_images, uac_contents = _uac_inventory(
            login, campaign_ids, uac_read_client_factory=uac_read_client_factory)
    except Exception as e:  # noqa: BLE001
        errors.append(f"инвентарь UAC: {str(e)[:180]}")

    # 2) Заливка новых файлов — по одному разу на пару, а не на объявление.
    # Один транспорт на кампанию: tp6/tp7 пишет только UAC-лег (см. _uac_owned_cids),
    # остальное — Grid. Поэтому и файл заливается только в те библиотеки, где
    # у пары реально есть работа: раньше пара, видимая обоим, лила его дважды.
    uac_owned = _uac_owned_cids(uac_contents)
    rsya_map: dict[str, str] = {}                 # старый хэш → новый хэш
    uac_map: dict[int, dict[str, str]] = {}       # cid → {старый content_id: новый}
    uac_old_hashes: dict[int, set[str]] = {}      # cid → заменяемые хэши (для проверки)
    for pair in pairs:
        old_key = str(pair.get("old_key") or "")
        tmp_path = str(pair.get("tmp_path") or "")
        if not old_key or not tmp_path or not os.path.isfile(tmp_path):
            errors.append(f"{old_key or '?'}: временный файл не найден")
            continue
        # Транспорт считаем ПО ОБЪЯВЛЕНИЯМ, а не по кампаниям: запрет Grid-лега на
        # UAC-владеемой кампании относится только к адаптивным (см. _grid_transport_scan).
        scan = _grid_transport_scan(ads_by_id, ad_cid, old_key, uac_owned)
        need_rsya = bool(scan["cids"])
        need_uac = old_key in uac_images
        if not need_rsya and not need_uac:
            if old_key in rsya_images:
                # Транспорта нет ни одного. Молча выдавать это за успех нельзя —
                # но и доходить сюда штатно не должно: инвентарь помечает такие
                # карточки ``supported: false`` (см. _annotate_transport), так что
                # это уже гонка «инвентарь изменился между показом и заданием».
                errors.append(
                    f"{old_key}: замена невозможна — "
                    + ("объявления с турболендингом/мультикарточками"
                       if scan["blocked_unsafe"] and not scan["blocked_uac"]
                       else "картинка только в адаптивных объявлениях tp6/tp7-кампаний, "
                            "а в их contents её нет"))
            else:
                errors.append(f"{old_key}: картинка не найдена в аккаунте")
            continue
        if need_rsya:
            try:
                grid = (grid_client_factory or rce._grid_client)(login)
                new_hash = grid.upload_image(tmp_path)
                if new_hash:
                    rsya_map[old_key] = str(new_hash)
                else:
                    errors.append(f"{old_key}: Grid upload_image вернул пустой хэш")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{old_key}: заливка в РСЯ — {str(e)[:180]}")
        if need_uac:
            try:
                client = rce._uac_client(login, uac_client_factory)
                new_content_id = str(client.upload_image_file(tmp_path))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{old_key}: заливка в UAC — {str(e)[:180]}")
                new_content_id = ""
            if new_content_id:
                # старый content_id внутри КАЖДОЙ кампании свой — ключ общий (хэш)
                for cid, contents in uac_contents.items():
                    for c in contents:
                        c_hash = str(c.get("direct_image_hash") or "")
                        c_id = str(c.get("id") or "")
                        if not c_id:
                            continue
                        if old_key == c_hash or old_key == f"uac:{c_id}":
                            uac_map.setdefault(int(cid), {})[c_id] = new_content_id
                            if c_hash:
                                uac_old_hashes.setdefault(int(cid), set()).add(c_hash)

    # 3) Запись. Grid-лег — только не-UAC кампании, UAC-лег — свои. Пересечения нет.
    replaced = 0
    result: dict = {}
    if rsya_map:
        out = _replace_rsya_images(login, rsya_map, ads_by_id, ad_cid,
                                   skip_cids=uac_owned,
                                   grid_client_factory=grid_client_factory)
        replaced += int(out.get("replaced") or 0)
        errors.extend(out.get("errors") or [])
        result["grid"] = out
    if uac_map:
        out = _replace_uac_images(login, uac_map, uac_client_factory=uac_client_factory)
        replaced += int(out.get("replaced") or 0)
        errors.extend(out.get("errors") or [])
        # Grid в эти кампании не писал — покрытие адаптивных объявлений МК
        # подтверждаем чтением, а не допущением.
        touched = {int(c) for c in (out.get("touched_ids") or [])}
        mirror = _verify_uac_mirror(
            login, {cid: hs for cid, hs in uac_old_hashes.items() if cid in touched},
            grid_client_factory=grid_client_factory)
        out["mirror_check"] = mirror
        errors.extend(mirror.get("errors") or [])
        for row in (mirror.get("stale") or []):
            errors.append(
                f"кампания {row['campaign_id']}: contents обновлены, но адаптивное "
                f"объявление ещё несёт старый хэш ({', '.join(row['hashes'])}) — "
                f"проверка сразу после PATCH, возможна задержка индексации Директа")
        result["uac"] = out

    # 4) Сверка легов. Grid отдал объявления UAC-легу по кампаниям ``left_to_uac_cids``
    # — но «отдал» ещё не значит «UAC их записал». Смешанный случай (хэш есть и в
    # не-UAC кампании, и в UAC-владеемой, которую UAC не покрыл) раньше выглядел
    # успехом: Grid писал в не-UAC, объявление МК пропускалось, а ``errors`` был пуст.
    # Считаем разницу «кому отдали» минус «где UAC реально отработал» и кладём в ошибки.
    left_cids = {int(c) for c in ((result.get("grid") or {}).get("left_to_uac_cids") or [])}
    if left_cids:
        uac_written = {int(c) for c in ((result.get("uac") or {}).get("touched_ids") or [])}
        unwritten = sorted(left_cids - uac_written)
        if unwritten:
            errors.append(
                "Grid-лег пропустил объявления кампаний "
                f"{', '.join(str(c) for c in unwritten)} как UAC-владеемые, но UAC-лег "
                "в них не отработал — замена в этих кампаниях НЕ выполнена")
        result["legs_reconcile"] = {
            "left_to_uac_cids": sorted(left_cids),
            "uac_written_cids": sorted(uac_written),
            "unwritten_cids": unwritten,
        }
    if skipped:
        result["skipped"] = skipped
    return {"replaced": replaced, "errors": errors, **result}


# ───────────────────────────── регистрация роутов ────────────────────────────

def register_image_routes(
    bp,
    access,
    *,
    v5_call: Callable,
    _login_allowed: Callable,
    _admin_allowed: Callable,
    _token: Callable,
    _enqueue_content_job: Callable,
) -> None:
    """Регистрирует ручки вкладки «Смена изображения» на blueprint ``bp``."""

    def _deny():
        """Вкладка админская: единый гейт для всех ручек."""
        if not _admin_allowed():
            return jsonify({"error": "Forbidden"}), 403
        return None

    def _scoped_login(raw_login: str):
        """(login, error_response|None) — скоуп директологов + наличие токена."""
        login = (raw_login or "").strip()
        if not login:
            return "", (jsonify({"error": "login обязателен"}), 400)
        ok, scope_err = _login_allowed(login)
        if not ok:
            return login, (jsonify({"error": scope_err}), 403)
        return login, None

    # ── Инвентарь картинок аккаунта ──────────────────────────────────────────
    @bp.route("/api/content-editor/images/inventory")
    @access
    def ce_images_inventory():
        deny = _deny()
        if deny is not None:
            return deny
        login, err = _scoped_login(request.args.get("login") or "")
        if err is not None:
            return err
        campaign_ids = _parse_campaign_ids(request.args.get("campaign_ids") or "")
        token, _agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404
        skipped: list[dict] = []
        rsya, uac = {}, {}
        ads_by_id, ad_cid, contents = {}, {}, {}
        try:
            rsya, ads_by_id, ad_cid, skipped = _rsya_inventory(
                token, login, v5_call, campaign_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"инвентарь РСЯ: {str(e)[:200]}"}), 502
        try:
            uac, contents = _uac_inventory(login, campaign_ids)
        except Exception as e:  # noqa: BLE001 — UAC = обогащение, не блокируем РСЯ
            print(f"[content-images] UAC inventory failed login={login}: {e!r}", flush=True)
            skipped.append({"reason": f"UAC (tp6/tp7) не прочитан: {str(e)[:120]}", "count": 0})
        # Транспорт известен уже здесь — карточка без него обязана приехать на фронт
        # как supported:false, а не выясняться ошибкой после постановки задания.
        images = _annotate_transport(_merge_inventory(rsya, uac), ads_by_id, ad_cid,
                                     uac, _uac_owned_cids(contents))
        return jsonify({"images": images, "skipped": skipped})

    # ── Загрузка новых файлов во временную папку ─────────────────────────────
    @bp.route("/api/content-editor/images/upload", methods=["POST"])
    @access
    def ce_images_upload():
        deny = _deny()
        if deny is not None:
            return deny
        login, err = _scoped_login(request.form.get("login") or "")
        if err is not None:
            return err
        _sweep_tmp()
        files = request.files.getlist("files[]") or request.files.getlist("files")
        if not files:
            return jsonify({"error": "не передан ни один файл"}), 400
        uploaded: list[dict] = []
        errors: list[dict] = []
        if len(files) > _MAX_FILES:
            errors.append({"name": "__request__",
                           "error": f"лимит {_MAX_FILES} файлов на запрос, лишние отброшены"})
            files = files[:_MAX_FILES]
        total = 0
        for fs in files:
            name = fs.filename or "image"
            try:
                raw = fs.read()
            except Exception as e:  # noqa: BLE001
                errors.append({"name": name, "error": f"чтение: {str(e)[:160]}"})
                continue
            total += len(raw or b"")
            if total > _MAX_BYTES:
                errors.append({"name": name,
                               "error": f"превышен суммарный лимит {_MAX_BYTES >> 20} МБ"})
                break
            try:
                jpeg, width, height = _to_jpeg(raw)
            except ImportError:
                return jsonify({"error": "Pillow не установлен на сервере"}), 503
            except Exception as e:  # noqa: BLE001
                errors.append({"name": name, "error": f"Pillow: {str(e)[:160]}"})
                continue
            # Лимит — на РЕЗУЛЬТАТ сжатия (q80 JPEG), не на исходник: q80 обычно
            # ужимает в разы, но не гарантированно (уже-JPEG высокого разрешения
            # почти не сжимается дальше). Директ сам ограничивает загружаемые
            # картинки по размеру — 10 МБ с запасом ниже любого известного лимита.
            if len(jpeg) > _MAX_OUTPUT_BYTES:
                errors.append({"name": name,
                               "error": f"после сжатия {len(jpeg) >> 20} МБ — "
                                        f"больше лимита {_MAX_OUTPUT_BYTES >> 20} МБ"})
                continue
            tmp_id = uuid.uuid4().hex[:24]
            path = _tmp_path(tmp_id)
            try:
                with open(path, "wb") as fh:
                    fh.write(jpeg)
            except OSError as e:
                errors.append({"name": name, "error": f"запись во временную папку: {str(e)[:160]}"})
                continue
            uploaded.append({
                "tmp_id": tmp_id, "filename": name,
                "preview_url": f"/direct/api/content-editor/images/tmp/{tmp_id}",
                "width": width, "height": height,
            })
        return jsonify({"uploaded": uploaded, "errors": errors})

    @bp.route("/api/content-editor/images/tmp/<tmp_id>")
    @access
    def ce_images_tmp(tmp_id):
        """Отдать превью только что загруженного (ещё не залитого в Директ) файла."""
        deny = _deny()
        if deny is not None:
            return deny
        path = _tmp_path(tmp_id)
        if not path or not os.path.isfile(path):
            return jsonify({"error": "not found"}), 404
        return send_file(path, mimetype="image/jpeg")

    # Превью «сколько объявлений/кампаний затронет замена» больше не отдельный эндпоинт:
    # /images/inventory уже отдаёт usages.campaigns на каждую картинку, фронт считает
    # affected/total_ads из уже загруженных данных (ceImgComputePreview в content_editor.js).
    # Раньше здесь был POST-запрос, повторяющий ПОЛНЫЙ _rsya_inventory — на аккаунте
    # с большим количеством кампаний это тот же ~минутный обход Директа, что и первичная
    # загрузка, и всё это время попап подтверждения не открывался («клик ничего не делает»).

    # ── Постановка замены в общую очередь content_jobs ───────────────────────
    @bp.route("/api/content-editor/images/replace_async", methods=["POST"])
    @access
    def ce_images_replace_async():
        deny = _deny()
        if deny is not None:
            return deny
        body = request.json or {}
        login, err = _scoped_login(body.get("login") or "")
        if err is not None:
            return err
        campaign_ids = _parse_campaign_ids(body.get("campaign_ids"))
        pairs: list[dict] = []
        for p in (body.get("pairs") or []):
            if not isinstance(p, dict):
                continue
            old_key = str(p.get("old_key") or "").strip()
            tmp_id = str(p.get("tmp_id") or "").strip()
            path = _tmp_path(tmp_id)
            if not old_key or not path:
                continue
            if not os.path.isfile(path):
                return jsonify({"error": f"загруженный файл {tmp_id} не найден — "
                                         f"перезагрузите картинку"}), 400
            # В джобу кладём ПУТЬ, а не байты: очередь в Postgres, тело задания
            # не должно распухать на мегабайты картинок.
            pairs.append({"old_key": old_key, "tmp_id": tmp_id, "tmp_path": path,
                          "filename": str(p.get("filename") or "")})
        if not pairs:
            return jsonify({"error": "не выбрана ни одна пара «картинка → замена»"}), 400
        _, agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404
        payload = json.dumps({"campaign_ids": campaign_ids, "pairs": pairs},
                             ensure_ascii=False)
        return _enqueue_content_job(
            login, agency, "image_replace", payload, payload, "image",
            len(campaign_ids), resp_type="image_replace")

"""Роуты вкладки «Смена изображения» редактора контента Директа (admin-only).

Отдельный модуль по образцу ``content_price_check_routes`` — ``routes_content_editor``
уже 2900+ строк, раздувать его нечем.

Что делает вкладка: показывает инвентарь картинок аккаунта (РСЯ-объявления + креативы
UAC/МК), даёт загрузить новый файл и заменить им выбранную картинку 1:1 во всех
затронутых объявлениях/кампаниях аккаунта (опционально — только в выбранных кампаниях).

Два транспорта записи, как и во всём редакторе:
  * РСЯ — cookie/Grid: ``GridClient.upload_image`` → ``update_ad_images``
    (старый хэш меняется на новый В ТОЙ ЖЕ ПОЗИЦИИ ``imageHashes``);
  * UAC (tp6/tp7) — cookie web-api: ``UacClient.upload_image_file`` → PATCH
    ``/uac/campaign/{id}`` со списком ``content_ids`` (позиция сохраняется).

Баллы v5 НЕ тратятся: ``adimages.get`` не вызывается нигде — инвентарь строится из
уже читаемых Grid-полей объявления и из ``contents[]`` детали UAC-кампании.
"""

from __future__ import annotations

import json
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
_TMP_TTL_SEC = 24 * 3600       # временные файлы живут сутки

# Поиск (tp2/tp4) картинок не имеет вовсе — честно сообщаем, а не молчим.
_SEARCH_TPS = {"tp2", "tp4"}
_SEARCH_SKIP_REASON = "поиск tp2/tp4 — картинки не поддерживаются"


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
                "name": str(img.get("name") or ""),
                "preview_url": str(img.get("preview_url") or ""),
                "width": int(img.get("width") or 0),
                "height": int(img.get("height") or 0),
            }
    return out


def _rsya_inventory(token: str, login: str, v5_call: Callable,
                    campaign_ids: list[int], *,
                    grid_client_factory: Callable | None = None) -> tuple[dict, dict, dict, list[dict]]:
    """Инвентарь картинок РСЯ-объявлений.

    → (images_by_hash, ads_by_id, ad_cid, skipped). ``ads_by_id`` — полный
    RMW-снимок объявления из ``adaptive_ads_for_update`` (нужен и превью, и
    записи), ``ad_cid`` — ad_id → campaign_id.
    """
    from . import routes_content_editor as rce

    camps, err = rce._v5_paginate(
        v5_call, "campaigns", token, login,
        rce._strip_campaign_subfield_names(
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"]}
        ),
        "Campaigns",
    )
    if err:
        raise RuntimeError(f"campaigns.get: {err}")
    camp: dict[int, dict] = {}
    for c in camps:
        cid = int(c.get("Id") or 0)
        if not cid or (campaign_ids and cid not in campaign_ids):
            continue
        name = c.get("Name") or ""
        camp[cid] = {"id": str(cid), "name": name,
                     "tp": _tp_from_campaign(name, c.get("Type") or "")}
    skipped: list[dict] = []
    search_cids = [cid for cid, c in camp.items() if c["tp"] in _SEARCH_TPS]
    if search_cids:
        skipped.append({"reason": _SEARCH_SKIP_REASON, "count": len(search_cids)})
    work_cids = [cid for cid in camp if cid not in set(search_cids)]
    if not work_cids:
        return {}, {}, {}, skipped

    ads, err = rce._v5_paginate_campaign_batches(
        v5_call, "ads", token, login,
        {"FieldNames": ["Id", "CampaignId", "Type", "State"]},
        "Ads",
        work_cids,
    )
    if err:
        raise RuntimeError(f"ads.get: {err}")
    ad_cid: dict[int, int] = {}
    for a in ads:
        aid = int(a.get("Id") or 0)
        cid = int(a.get("CampaignId") or 0)
        if aid > 0 and cid in camp:
            ad_cid[aid] = cid
    if not ad_cid:
        return {}, {}, {}, skipped

    grid = (grid_client_factory or rce._grid_client)(login)
    ads_by_id = grid.adaptive_ads_for_update(work_cids, list(ad_cid)) or {}

    images: dict[str, dict] = {}
    for aid, item in ads_by_id.items():
        cid = ad_cid.get(int(aid))
        if not cid:
            continue
        for h in (item.get("imageHashes") or []):
            h = str(h)
            if not h:
                continue
            entry = images.setdefault(h, {
                "key": h, "hash": h, "name": "", "preview_url": "",
                "width": 0, "height": 0, "source": "rsya",
                "usages": {"ads": 0, "campaigns": []},
                "supported": True, "reason": "",
            })
            entry["usages"]["ads"] += 1
            row = next((c for c in entry["usages"]["campaigns"] if c["id"] == str(cid)), None)
            if row is None:
                row = {**camp[cid], "ads": 0}
                entry["usages"]["campaigns"].append(row)
            row["ads"] += 1

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
                "key": key, "hash": img_hash, "name": str(c.get("filename") or ""),
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
            for field in ("name", "preview_url"):
                if not cur.get(field) and entry.get(field):
                    cur[field] = entry[field]
            for field in ("width", "height"):
                if not cur.get(field) and entry.get(field):
                    cur[field] = entry[field]
    return sorted(out.values(), key=lambda e: (-e["usages"]["ads"], e["key"]))


# ───────────────────────────── исполнение замены ─────────────────────────────

def _replace_rsya_images(login: str, pairs_hashes: dict[str, str],
                         ads_by_id: dict, ad_cid: dict[int, int],
                         *, grid_client_factory: Callable | None = None) -> dict:
    """Заменить старые хэши на новые ВО ВСЕХ затронутых объявлениях РСЯ.

    ``pairs_hashes``: {старый хэш: новый хэш}. Позиция в ``imageHashes``
    сохраняется — полная замена списка Grid'ом иначе перетасует картинки.
    """
    from . import routes_content_editor as rce

    items: list[dict] = []
    touched_campaigns: set[int] = set()
    for aid, ad in (ads_by_id or {}).items():
        hashes = list(ad.get("imageHashes") or [])
        if not any(h in pairs_hashes for h in hashes):
            continue
        new_hashes = [pairs_hashes.get(h, h) for h in hashes]   # позиции 1:1
        item = dict(ad)
        item["imageHashes"] = new_hashes
        items.append(item)
        cid = ad_cid.get(int(aid))
        if cid:
            touched_campaigns.add(cid)
    if not items:
        return {"replaced": 0, "errors": [], "campaigns_touched": 0}
    grid = (grid_client_factory or rce._grid_client)(login)
    updated = grid.update_ad_images(items)
    errors: list[str] = []
    if updated < len(items):
        errors.append(f"Grid обновил {updated} объявл. из {len(items)}")
    return {"replaced": int(updated), "errors": errors,
            "campaigns_touched": len(touched_campaigns)}


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
    touched = 0
    for cid, mapping in pairs_content.items():
        try:
            detail = rce._unwrap_uac_response(
                client._request("GET", f"/campaign/{cid}", step=f"uac-detail:{cid}"))
            current = [str((c or {}).get("id") or "")
                       for c in (detail.get("contents") or []) if isinstance(c, dict)]
            current = [c for c in current if c]
            if not any(c in mapping for c in current):
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
            touched += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"кампания {cid}: {str(e)[:180]}")
    return {"replaced": replaced, "errors": errors, "campaigns_touched": touched}


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
    rsya_map: dict[str, str] = {}                 # старый хэш → новый хэш
    uac_map: dict[int, dict[str, str]] = {}       # cid → {старый content_id: новый}
    for pair in pairs:
        old_key = str(pair.get("old_key") or "")
        tmp_path = str(pair.get("tmp_path") or "")
        if not old_key or not tmp_path or not os.path.isfile(tmp_path):
            errors.append(f"{old_key or '?'}: временный файл не найден")
            continue
        need_rsya = old_key in rsya_images
        need_uac = old_key in uac_images
        if not need_rsya and not need_uac:
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

    # 3) Запись.
    replaced = 0
    result: dict = {}
    if rsya_map:
        out = _replace_rsya_images(login, rsya_map, ads_by_id, ad_cid,
                                   grid_client_factory=grid_client_factory)
        replaced += int(out.get("replaced") or 0)
        errors.extend(out.get("errors") or [])
        result["grid"] = out
    if uac_map:
        out = _replace_uac_images(login, uac_map, uac_client_factory=uac_client_factory)
        replaced += int(out.get("replaced") or 0)
        errors.extend(out.get("errors") or [])
        result["uac"] = out
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
        try:
            rsya, _ads, _ad_cid, skipped = _rsya_inventory(token, login, v5_call, campaign_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"инвентарь РСЯ: {str(e)[:200]}"}), 502
        try:
            uac, _contents = _uac_inventory(login, campaign_ids)
        except Exception as e:  # noqa: BLE001 — UAC = обогащение, не блокируем РСЯ
            print(f"[content-images] UAC inventory failed login={login}: {e!r}", flush=True)
            skipped.append({"reason": f"UAC (tp6/tp7) не прочитан: {str(e)[:120]}", "count": 0})
        return jsonify({"images": _merge_inventory(rsya, uac), "skipped": skipped})

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

    # ── Превью: сколько объявлений/кампаний затронет замена ──────────────────
    @bp.route("/api/content-editor/images/preview", methods=["POST"])
    @access
    def ce_images_preview():
        deny = _deny()
        if deny is not None:
            return deny
        body = request.json or {}
        login, err = _scoped_login(body.get("login") or "")
        if err is not None:
            return err
        campaign_ids = _parse_campaign_ids(body.get("campaign_ids"))
        keys = {str((p or {}).get("old_key") or "")
                for p in (body.get("pairs") or []) if isinstance(p, dict)}
        keys.discard("")
        if not keys:
            return jsonify({"error": "не выбрана ни одна картинка"}), 400
        token, _agency, terr = _token(login)
        if terr:
            return jsonify({"error": terr}), 404
        skipped: list[dict] = []
        try:
            rsya, _ads, _ad_cid, skipped = _rsya_inventory(token, login, v5_call, campaign_ids)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"инвентарь РСЯ: {str(e)[:200]}"}), 502
        try:
            uac, _contents = _uac_inventory(login, campaign_ids)
        except Exception as e:  # noqa: BLE001
            print(f"[content-images] UAC preview failed login={login}: {e!r}", flush=True)
            uac = {}
            skipped.append({"reason": f"UAC (tp6/tp7) не прочитан: {str(e)[:120]}", "count": 0})
        # Одна строка на кампанию: сколько объявлений в ней затронуто.
        affected: dict[str, dict] = {}
        total_ads = 0
        for entry in _merge_inventory(rsya, uac):
            if entry["key"] not in keys:
                continue
            total_ads += entry["usages"]["ads"]
            for camp in entry["usages"]["campaigns"]:
                row = affected.setdefault(camp["id"], {
                    "campaign_id": camp["id"], "campaign_name": camp["name"],
                    "tp": camp["tp"], "ads": 0,
                })
                row["ads"] += int(camp.get("ads") or 0)
        return jsonify({"affected": sorted(affected.values(), key=lambda r: -r["ads"]),
                        "total_ads": total_ads, "skipped": skipped})

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

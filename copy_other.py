"""Вспомогательные функции вкладки «Прочие сферы» (режим mode='other') сервиса copy_engine.

Вынесено из copy_engine.py ради читаемости (чистый рефактор, логика не изменена).

Архитектура DI:
- gf (grid_finalize) импортируется на уровне модуля: sibling-импорт, цикла нет.
- _resolve_agency_hint, _grid_feeds и _copy_feeds_preview берутся из copy_engine лениво
  (from . import copy_engine as _ce) внутри тел функций — только в runtime, не при загрузке
  модуля. Это исключает цикл: copy_engine импортирует copy_other на уровне модуля для
  ре-экспорта, а copy_other не импортирует copy_engine на уровне модуля.
- automation_runtime.py правок НЕ требует: configure() вызывается только для copy_engine,
  DI-глобалы copy_engine заполнены к моменту первого вызова любой функции этого модуля.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import grid_finalize as gf


# ── Константы ────────────────────────────────────────────────────────────────
_ARCHIVE_MAX_FILES = 500          # лимит файлов на запрос (все архивы+файлы суммарно)
_ARCHIVE_MAX_BYTES = 200 << 20    # 200 МБ суммарного распакованного размера на запрос
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})


# ── Feed auto-match helpers — ЕДИНСТВЕННАЯ реализация эвристики ──────────────
# Используются и в job (_copy_auto_feed_map), и в preview (_copy_feeds_check).
# Логика НЕ дублируется: обе точки входа вызывают _feed_auto_match_one.

def _feed_url_path(url: str) -> str:
    """URL без схемы и домена, lower-case (для сравнения путей)."""
    return re.sub(r"^https?://[^/]+", "", str(url or "")).lower()


def _feed_url_file(url: str) -> str:
    """Имя файла из URL без query-строки, lower-case."""
    url = str(url or "")
    return (url.split("/")[-1].split("?")[0]).lower()


def _feed_identity(feed: dict) -> str:
    return " ".join(str(feed.get(k) or "") for k in ("name", "url", "href", "source", "SourceUrl"))


def _feed_values(feed: dict) -> list[str]:
    return [
        str(feed.get(k) or "")
        for k in ("name", "url", "href", "source", "SourceUrl")
        if str(feed.get(k) or "").strip()
    ]


def _feed_has_listings(feed: dict) -> bool:
    return bool(feed.get("listings") or feed.get("Listings"))


def _fallback_target_feed(tgt_feeds: list[dict]) -> int | None:
    """Best-effort target feed when path/file matching fails.

    Used only after exact URL/path matching misses. Feeds with listing categories are already
    known by Grid and are safer than creating a new default URL feed that may parse as ERROR.
    """
    candidates = [f for f in tgt_feeds if _feed_has_listings(f)]
    if not candidates:
        return None

    def _score(feed: dict) -> tuple[int, int, int, int]:
        raw = _feed_identity(feed).lower()
        return (
            1 if "yandex" in raw else 0,
            1 if "used" in raw or "пробег" in raw else 0,
            1 if "auto" in raw else 0,
            len(feed.get("listings") or feed.get("Listings") or []),
        )

    best = sorted(candidates, key=_score, reverse=True)[0]
    try:
        fid = int(best.get("id") or 0)
    except (TypeError, ValueError):
        return None
    return fid or None


def _feed_auto_match_one(src_feed: dict, tgt_feeds: list[dict]) -> tuple:
    """Сопоставить один фид источника с фидами цели.

    Правило 1 'path': совпадение полного пути URL без домена (разные домены, один путь).
    Правило 2 'file': совпадение имени файла (basename, без query).
    Возвращает (tgt_id: int, rule: str) или (None, None) если совпадение не найдено."""
    def _feed_id(feed: dict) -> int | None:
        try:
            fid = int(feed.get("id") or 0)
        except (TypeError, ValueError):
            return None
        return fid or None

    src_paths = {_feed_url_path(v) for v in _feed_values(src_feed) if _feed_url_path(v)}
    src_files = {_feed_url_file(v) for v in _feed_values(src_feed) if _feed_url_file(v)}
    first_exact_match: tuple[int, str] | None = None

    path_matches = []
    for tf in tgt_feeds:
        tf_paths = {_feed_url_path(v) for v in _feed_values(tf) if _feed_url_path(v)}
        if src_paths and tf_paths and src_paths & tf_paths:
            path_matches.append(tf)
    if path_matches:
        for tf in path_matches:
            fid = _feed_id(tf)
            if fid is not None and _feed_has_listings(tf):
                return fid, "path"
        fid = _feed_id(path_matches[0])
        if fid is not None:
            first_exact_match = (fid, "path")

    file_matches = []
    for tf in tgt_feeds:
        tf_files = {_feed_url_file(v) for v in _feed_values(tf) if _feed_url_file(v)}
        if src_files and tf_files and src_files & tf_files:
            file_matches.append(tf)
    if file_matches:
        for tf in file_matches:
            fid = _feed_id(tf)
            if fid is not None and _feed_has_listings(tf):
                return fid, "file"
        if first_exact_match is None:
            fid = _feed_id(file_matches[0])
            if fid is not None:
                first_exact_match = (fid, "file")

    fallback_id = _fallback_target_feed(tgt_feeds)
    if fallback_id is not None:
        return fallback_id, "target_listing_fallback"
    if first_exact_match is not None:
        return first_exact_match
    return None, None


def _copy_grid_feeds_for_login(login: str, agency_hint: str = "") -> list[dict]:
    from . import copy_engine as _ce  # ленивый: copy_engine полностью загружен к этому моменту

    agency = (agency_hint or "").strip() or _ce._resolve_agency_hint(login, "")
    rows = _ce._grid_feeds(login, agency) or []
    return [{
        "id": int(f["id"]),
        "name": str(f.get("name") or ""),
        "url": str(f.get("url") or ""),
        "listings": f.get("listings") or [],
    }
            for f in rows if str(f.get("id") or "").strip().isdigit()]


def _copy_auto_feed_map_from_feeds(source_feeds: list[dict], target_feeds: list[dict]) -> dict[str, int]:
    feed_map: dict[str, int] = {}
    for sf in source_feeds:
        matched_id, _ = _feed_auto_match_one(sf, target_feeds)
        if matched_id is not None:
            feed_map[str(sf["id"])] = matched_id
    return feed_map


def _copy_snapshot_feed_rows(src_dir: Path) -> list[dict]:
    try:
        rows = json.loads((Path(src_dir) / "feeds.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for feed in rows:
        if not isinstance(feed, dict) or not str(feed.get("Id") or feed.get("id") or "").strip().isdigit():
            continue
        url_feed = feed.get("UrlFeed") if isinstance(feed.get("UrlFeed"), dict) else {}
        out.append({
            "id": int(feed.get("Id") or feed.get("id")),
            "name": str(feed.get("Name") or feed.get("name") or ""),
            "url": str(feed.get("url") or feed.get("Url") or url_feed.get("Url") or ""),
            "listings": [],
        })
    return out


def _copy_auto_feed_map_from_snapshot(
    src_dir: Path,
    target_login: str,
    *,
    target_agency_hint: str = "",
) -> dict[str, int]:
    """Auto feed-map using already pulled v5 source feeds instead of source Grid feeds."""
    try:
        source_feeds = _copy_snapshot_feed_rows(Path(src_dir))
        target_feeds = _copy_grid_feeds_for_login(target_login, target_agency_hint)
    except Exception:  # noqa: BLE001 — best-effort, same contract as _copy_auto_feed_map
        return {}
    return _copy_auto_feed_map_from_feeds(source_feeds, target_feeds)


def _copy_auto_feed_map(
    source_login: str,
    target_login: str,
    *,
    source_agency_hint: str = "",
    target_agency_hint: str = "",
) -> dict[str, int]:
    """Автоматическое сопоставление фидов источника → цель по URL/имени файла.
    Зеркало JS-функции _feedMatchTarget. Использует _feed_auto_match_one."""

    try:
        source_feeds = _copy_grid_feeds_for_login(source_login, source_agency_hint)
        target_feeds = _copy_grid_feeds_for_login(target_login, target_agency_hint)
    except Exception:  # noqa: BLE001 — best-effort
        return {}
    return _copy_auto_feed_map_from_feeds(source_feeds, target_feeds)


def _copy_feeds_check(source_login: str, target_login: str, selected_ids: set[int]) -> dict:
    """Сухой прогон автосопоставления фидов ДО запуска job.

    Реиспользует _copy_feeds_preview (те же фиды с usage-данными) и _feed_auto_match_one
    (та же эвристика, что применит _copy_auto_feed_map в job).
    Никаких дополнительных Grid-запросов: один вызов _copy_feeds_preview покрывает всё.
    Возвращает {matches, unmatched, source_total, target_total}."""
    from . import copy_engine as _ce  # ленивый: _copy_feeds_preview остаётся в copy_engine
    try:
        preview = _ce._copy_feeds_preview(source_login, target_login, selected_ids)
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось получить фиды: {str(e)[:200]}"}
    source_feeds = preview.get("source_feeds") or []
    target_feeds = preview.get("target_feeds") or []
    matches: list[dict] = []
    unmatched: list[dict] = []
    for sf in source_feeds:
        tgt_id, rule = _feed_auto_match_one(sf, target_feeds)
        if tgt_id is not None:
            tgt_feed = next((tf for tf in target_feeds if tf["id"] == tgt_id), None)
            matches.append({
                "src_id": sf["id"],
                "src_name": sf.get("name") or "",
                "tgt_id": tgt_id,
                "tgt_name": tgt_feed["name"] if tgt_feed else str(tgt_id),
                "rule": rule,
                "campaigns": sf.get("campaigns") or 0,
                "groups": sf.get("groups") or 0,
            })
        else:
            unmatched.append({
                "src_id": sf["id"],
                "src_name": sf.get("name") or "",
                "campaigns": sf.get("campaigns") or 0,
                "groups": sf.get("groups") or 0,
            })
    return {
        "matches": matches,
        "unmatched": unmatched,
        "source_total": len(source_feeds),
        "target_total": len(target_feeds),
    }


def _extract_archive_images(filename: str, file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[dict]]:
    """Извлечь изображения из ZIP или TAR/TAR.GZ архива.

    Охранники (недоверенный ввод):
    - zip-slip: пути с '..', абсолютные — пропускаются.
    - симлинки — пропускаются (ZIP external_attr, TAR issym/islnk).
    - __MACOSX/, .DS_Store, файлы с '.' в начале — пропускаются.
    - не-изображения (расширение не в _IMAGE_EXTS) — пропускаются молча.
    - одиночный файл > 10 МБ — пропускается молча.
    - лимит {_ARCHIVE_MAX_FILES} файлов и {_ARCHIVE_MAX_BYTES >> 20} МБ суммарно.
    .rar — не поддерживается (закрытый формат), возвращает понятную ошибку.

    Порядок: сортировка по имени файла — детерминированный round-robin раскладки.
    Возвращает (images, errors): images = [(basename, raw_bytes)]."""
    import io as _io
    import os as _os
    import tarfile as _tarfile
    import zipfile as _zipfile

    fname_lower = (filename or "").lower()
    images: list[tuple[str, bytes]] = []
    errors: list[dict] = []
    total_bytes = 0

    def _img_ok(raw: str) -> bool:
        base = _os.path.basename(raw.replace("\\", "/"))
        if not base or base.startswith("."):
            return False
        if "__MACOSX" in raw or ".DS_Store" in base:
            return False
        return _os.path.splitext(base)[1].lower() in _IMAGE_EXTS

    def _base(raw: str) -> str:
        return _os.path.basename(raw.replace("\\", "/"))

    def _traversal(raw: str) -> bool:
        return raw.startswith("/") or ".." in raw.replace("\\", "/").split("/")

    if fname_lower.endswith(".rar"):
        return [], [{"name": filename,
                     "error": "формат .rar не поддерживается — используйте zip или tar.gz"}]

    if fname_lower.endswith(".zip"):
        try:
            with _zipfile.ZipFile(_io.BytesIO(file_bytes)) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename):
                    if info.is_dir() or _traversal(info.filename):
                        continue
                    if (info.external_attr >> 16) & 0xF000 == 0xA000:  # symlink
                        continue
                    if not _img_ok(info.filename):
                        continue
                    if len(images) >= _ARCHIVE_MAX_FILES:
                        errors.append({"name": filename,
                                       "error": f"лимит {_ARCHIVE_MAX_FILES} файлов в архиве"})
                        break
                    if info.file_size > 10 << 20:
                        continue
                    if total_bytes + info.file_size > _ARCHIVE_MAX_BYTES:
                        errors.append({"name": filename,
                                       "error": f"суммарный размер превышает {_ARCHIVE_MAX_BYTES >> 20} МБ"})
                        break
                    total_bytes += info.file_size
                    try:
                        images.append((_base(info.filename), zf.read(info.filename)))
                    except Exception as ex:  # noqa: BLE001
                        errors.append({"name": _base(info.filename), "error": f"zip read: {str(ex)[:100]}"})
        except Exception as ex:  # noqa: BLE001
            return [], [{"name": filename, "error": f"zip: {str(ex)[:200]}"}]

    elif fname_lower.endswith((".tar", ".tar.gz", ".tgz")):
        try:
            mode = "r:gz" if fname_lower.endswith((".tar.gz", ".tgz")) else "r:"
            with _tarfile.open(fileobj=_io.BytesIO(file_bytes), mode=mode) as tf:
                for member in sorted(tf.getmembers(), key=lambda m: m.name):
                    if not member.isfile() or _traversal(member.name):
                        continue
                    if member.issym() or member.islnk():
                        continue
                    if not _img_ok(member.name):
                        continue
                    if len(images) >= _ARCHIVE_MAX_FILES:
                        errors.append({"name": filename,
                                       "error": f"лимит {_ARCHIVE_MAX_FILES} файлов в архиве"})
                        break
                    if member.size > 10 << 20:
                        continue
                    if total_bytes + member.size > _ARCHIVE_MAX_BYTES:
                        errors.append({"name": filename,
                                       "error": f"суммарный размер превышает {_ARCHIVE_MAX_BYTES >> 20} МБ"})
                        break
                    total_bytes += member.size
                    try:
                        fh = tf.extractfile(member)
                        if fh:
                            images.append((_base(member.name), fh.read()))
                    except Exception as ex:  # noqa: BLE001
                        errors.append({"name": _base(member.name), "error": f"tar read: {str(ex)[:100]}"})
        except Exception as ex:  # noqa: BLE001
            return [], [{"name": filename, "error": f"tar: {str(ex)[:200]}"}]

    else:
        return [], [{"name": filename, "error": f"неизвестный формат архива: {filename}"}]

    return images, errors


def _copy_images_upload(target_login: str, files_data: list) -> dict:
    """Загрузить изображения (отдельные файлы или архивы ZIP/TAR) в аккаунт target.

    Поддерживаемые форматы: JPEG, PNG, WEBP, GIF (первый кадр), BMP, а также архивы:
      .zip, .tar, .tar.gz, .tgz — через stdlib (без новых зависимостей).
    .rar — не поддерживается, возвращается понятная ошибка.
    Архивы распаковываются через _extract_archive_images с zip-slip/symlink-защитой.
    Порядок из архива: сортировка по имени (детерминированный round-robin).

    Каждое изображение: Pillow → RGB → JPEG quality=80 → GridClient.upload_image().
    Возвращает {results:[{name,hash}], errors:[...], archive_extracted: int}."""
    try:
        from PIL import Image as _PIL_Image  # noqa: N813
        import io as _io
    except ImportError:
        return {"error": "Pillow не установлен на сервере — обратитесь к администратору", "code": 503}

    import os as _os
    import tempfile as _tempfile

    results: list[dict] = []
    errors: list[dict] = []

    try:
        tgt_grid = gf.GridClient(target_login)
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось подключиться к аккаунту {target_login}: {str(e)[:200]}", "code": 502}

    _ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".rar")
    archive_extracted = 0

    # Шаг 1: раскрываем архивы в плоский список (filename, bytes)
    expanded: list[tuple[str, bytes]] = []
    for filename, file_bytes in (files_data or []):
        fname_lower = (filename or "").lower()
        if any(fname_lower.endswith(s) for s in _ARCHIVE_SUFFIXES):
            imgs, errs = _extract_archive_images(filename, file_bytes)
            expanded.extend(imgs)
            errors.extend(errs)
            archive_extracted += len(imgs)
        else:
            expanded.append((filename, file_bytes))

    # Сквозные лимиты по ВСЕМУ запросу (не per-archive).
    # _ARCHIVE_MAX_FILES / _ARCHIVE_MAX_BYTES — те же константы, что и для отдельного архива;
    # перемножение архивов 10×200 здесь не пройдёт.
    if len(expanded) > _ARCHIVE_MAX_FILES:
        excess = len(expanded) - _ARCHIVE_MAX_FILES
        errors.append({"name": "__request__",
                        "error": f"суммарный лимит запроса: {_ARCHIVE_MAX_FILES} изображений, "
                                 f"лишние {excess} отброшены"})
        expanded = expanded[:_ARCHIVE_MAX_FILES]
    request_bytes = sum(len(b) for _, b in expanded)
    if request_bytes > _ARCHIVE_MAX_BYTES:
        return {"results": [], "errors": errors + [
            {"name": "__request__",
             "error": f"суммарный объём {request_bytes >> 20} МБ превышает лимит "
                      f"{_ARCHIVE_MAX_BYTES >> 20} МБ на запрос"}
        ], "archive_extracted": archive_extracted}

    # Шаг 1.5: схлопываем дубли 1:1 по имени файла (регистронезависимо).
    # Один и тот же файл, попавший дважды (или лежащий и в архиве, и отдельно),
    # не должен заливаться в Директ повторно: это лишние units, лишние картинки
    # в аккаунте и перекос round-robin раскладки по объявлениям.
    _seen_names: dict = {}
    _deduped: list = []
    dup_collapsed = 0
    for filename, file_bytes in expanded:
        key = (filename or "").strip().lower()
        if key in _seen_names:
            dup_collapsed += 1
            continue
        _seen_names[key] = True
        _deduped.append((filename, file_bytes))
    expanded = _deduped

    # Шаг 2: конвертируем и заливаем все изображения
    for filename, file_bytes in expanded:
        try:
            img = _PIL_Image.open(_io.BytesIO(file_bytes))
            # Композитинг на белый фон перед конвертацией в JPEG (который не поддерживает альфа).
            # Простой img.convert("RGB") отбрасывает альфа-канал → прозрачные пиксели станут
            # чёрными (RGB 0,0,0) → логотипы с прозрачным фоном превращаются в чёрные квадраты.
            # Обрабатываем все режимы с альфой: RGBA, LA, PA, P с transparency.
            has_alpha = (img.mode in ("RGBA", "LA", "PA")
                         or (img.mode == "P" and "transparency" in img.info))
            if has_alpha:
                img = img.convert("RGBA")
                bg = _PIL_Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])   # split()[-1] = alpha channel
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            jpeg_bytes = buf.getvalue()
        except Exception as e:  # noqa: BLE001
            errors.append({"name": filename, "error": f"Pillow: {str(e)[:200]}"})
            continue
        try:
            fd, tmp_path = _tempfile.mkstemp(suffix=".jpg")
            try:
                with _os.fdopen(fd, "wb") as fh:
                    fh.write(jpeg_bytes)
                img_hash = tgt_grid.upload_image(tmp_path)
            finally:
                try:
                    _os.unlink(tmp_path)
                except Exception:  # noqa: BLE001
                    pass
            if img_hash:
                results.append({"name": filename, "hash": img_hash})
            else:
                errors.append({"name": filename, "error": "upload_image вернул пустой хэш"})
        except Exception as e:  # noqa: BLE001
            errors.append({"name": filename, "error": f"upload: {str(e)[:200]}"})

    return {"results": results, "errors": errors, "archive_extracted": archive_extracted,
            "dup_collapsed": dup_collapsed}

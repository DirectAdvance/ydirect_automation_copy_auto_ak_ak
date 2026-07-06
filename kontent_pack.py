"""Чтение контент-пака нейродиректолога с M3 (sshfs-монт /opt/neuro_kontent).

КЛЮЧЕВОЕ: папки пака ``kontent_oktyabr/<сегмент>/tpN/ctNNNN/`` закодированы РОВНО
нашим каноническим кодом ``public.local_gsheet_naming`` (проверено: feed-имена
``ct0031_id69_Changan_CS35Plus`` == ``ag_part1`` в БД). Поэтому моста-по-имени НЕ
нужно — ``ct`` нашей группы = имя папки пака напрямую.

(⚠️ НЕ путать с ``data/coder_autosalons_v16__нейминг.csv`` — там СТАРАЯ/краснодарская
нумерация ct, она НЕ совпадает с папками пака и для индексации НЕ используется.)

Каждая ct-папка: ``keywords/<slepok>.txt`` (+ ``_minus``/``_minus_shared``),
``callouts/<slepok>.txt``, ``image.txt`` (манифест фид-картинок → ``_image_store/feeds``).
``ct0000`` = «полное отсутствие ключей» (общий мультибренд).

Модуль БЕЗ БД/Flask: на вход — наш ``ct`` (+ сегмент, tp, slepok), на выход —
тексты/пути. Читает живьём с монта точечно (без рекурсивных сканов: sshfs через
двойной хоп медленный на обход, но мгновенный на конкретный путь).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import math
import shutil

# NEURO_PACK_MOUNT: переключение на ЛОКАЛЬНУЮ копию пака (перенесённую с M3 ночным
# sync-ом, scripts/sync_content_m3.py). Дефолт — sshfs-монт (обратная совместимость).
PACK_MOUNT = os.environ.get("NEURO_PACK_MOUNT", "/opt/neuro_kontent")
PACK_ROOT = os.path.join(PACK_MOUNT, "kontent_oktyabr")
FEEDS_DIR = os.path.join(PACK_ROOT, "_image_store", "feeds")

# Путь пака НА САМОЙ M3 (для батч-сбора через ssh — скрипт читает локальный диск M3)
M3_RELAY = "m3-relay"
M3_AGENCY_ROOT = "/Users/Shared/agency/нейродиректолог"
M3_PACK_ROOT = M3_AGENCY_ROOT + "/kontent_oktyabr"
M3_MANUAL_ROOT = "/Users/Shared/agency/creatives/Manual"
# Корень per-ct видео-пула на M3 (agency/Video/<ct>/*.mp4).
# Локальная копия (сжатая, валидная) синкается компрессором sync_content_m3.py в
# NEURO_LOCAL_DST/_video_pool/ под тем же деревом. Маппинг: M3_VIDEO_ROOT/<rel> → _video_pool/<rel>.
M3_VIDEO_ROOT = "/Users/Shared/agency/Video"

# Локальная зеркальная копия пака (перенос на Proxmox, scripts/sync_content_m3.py). Если
# NEURO_PACK_MOUNT указывает на ЛОКАЛЬНУЮ папку (не sshfs-монт /opt/neuro_kontent), маппим
# M3-путь (_fetch_bytes) на неё и читаем байты БЕЗ ssh — в разы быстрее image/video-heavy
# наборов. None (дефолтный sshfs) → прежнее поведение (ssh cat с M3). Байты картинок/видео
# раньше шли ТОЛЬКО через ssh, минуя перенос — этот маппинг и делает перенос эффективным.
_LOCAL_MIRROR_ROOT = PACK_MOUNT if (PACK_MOUNT != "/opt/neuro_kontent" and os.path.isdir(PACK_MOUNT)) else None

# ── АНТИ-ЗАВИСАНИЕ sshfs: локальный ИНДЕКС (структура) + точечный фетч байтов ──
# Корень зависаний — «слепой» обход каталогов по sshfs (os.listdir/find без таймаута):
# когда Mac (m3-relay) загружен, FUSE-чтение стопорится навсегда и морозит весь воркер.
# Решение (как просил пользователь): храним ЛОКАЛЬНО только МАЛЕНЬКУЮ часть —
#   1) ИНДЕКС/структуру (manifest.json: имена файлов картинок/видео + per-ct манифесты),
#      строится ОДНИМ ssh-скриптом НА M3 (её локальный диск мгновенный) с таймаутом;
#   2) БАЙТЫ нужных картинок/видео тянем ТОЧЕЧНО по известному пути (scp+timeout) в
#      небольшой LRU-кэш. Никаких os.listdir по sshfs → нет бесконечных зависаний.
import hashlib
import posixpath
import time as _time
import base64
import struct
import zlib

INDEX_DIR = "/opt/neuro_kontent_index"
INDEX_PATH = os.path.join(INDEX_DIR, "manifest.json")
INDEX_MAX_AGE = 3600                  # сек: считаем индекс свежим до 1 ч (таймер обновляет чаще)
CACHE_DIR = "/opt/neuro_kontent_cache"
THUMB_CACHE_DIR = "/opt/neuro_kontent_thumb_cache"
CACHE_CAP_MB = 2048                   # лимит LRU-кэша байтов (растёт ограниченно, а не как весь пак)
YANDEX_VIDEO_MAX = int(9.9 * 1024 * 1024)  # лимит Яндекс.Директа на видео (9.9 МБ)
YANDEX_VIDEO_MIN_DURATION = 5.0       # сек: короче Яндекс отклоняет upload HTTP 400 (живой кейс ct0024)
_VIDEO_CANDIDATE_CAP = 8              # сколько кандидатов пробовать на выбор limit валидных (см. ниже)
# SSH-мультиплексирование (ControlMaster): первое соединение держится 5 мин и переиспользуется
# всеми ssh cat → нет рукопожатия на КАЖДЫЙ файл (было ~3.5с/файл → станет ~0.1с). Критично для
# пакетной выгрузки картинок/видео при создании РК.
_SSH_MUX = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/.neuro_m3_%C", "-o", "ControlPersist=300"]
_M3_SSH = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", *_SSH_MUX, M3_RELAY]
_M3_SSH_PLAIN = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", M3_RELAY]
_FETCH_TIMEOUT = 90                   # сек на один файл (потом — провал, не зависание)
_FETCH_WORKERS = 8                    # параллельных скачиваний байтов (через mux-канал)
_INDEX_CACHE: dict = {"mtime": -1, "data": None}
_REMOTE_AHASH_CACHE: dict[str, int | None] = {}
_REMOTE_PHASH_CACHE: dict[str, int | None] = {}
_PHASH_COS_CACHE: dict[int, list[list[float]]] = {}
_CONTENT_TREE_CACHE: dict = {"mtime": -1, "data": None}

# Скрипт строит индекс НА M3 (локальный диск Mac → мгновенно, без sshfs-обхода).
# Только имена/манифесты (мелочь), НЕ байты. corpus* в _slepki_data не трогаем (не нужны).
_INDEX_BUILDER = r'''
import os, sys, json, re
root = sys.argv[1]
def rd(p):
    try:
        return [l.strip() for l in open(p, encoding="utf-8")
                if l.strip() and not l.lstrip().startswith("#")]
    except Exception:
        return []
out = {"feeds": [], "packs": {}, "slepki_data": {}, "external_assets": {}, "external_folders": {}}
try:
    out["feeds"] = sorted(f for f in os.listdir(os.path.join(root, "_image_store", "feeds"))
                          if f.lower().endswith((".png", ".jpg", ".jpeg")))
except Exception:
    pass
for seg in (os.listdir(root) if os.path.isdir(root) else []):
    segp = os.path.join(root, seg)
    if seg.startswith("_") or not os.path.isdir(segp):
        continue
    for tp in os.listdir(segp):
        tpp = os.path.join(segp, tp)
        if not os.path.isdir(tpp):
            continue
        for ct in os.listdir(tpp):
            ctp = os.path.join(tpp, ct)
            if not os.path.isdir(ctp):
                continue
            d = {"_exists": True}
            for mf, key in (("image.txt", "image"), ("image_slepki.txt", "image_slepki"), ("video.txt", "video")):
                ls = rd(os.path.join(ctp, mf))
                if ls:
                    d[key] = ls
            direct_img = []
            try:
                direct_img = sorted(f for f in os.listdir(ctp) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
            except Exception:
                pass
            if direct_img:
                d["direct_image"] = direct_img
            mp4 = []
            try:
                mp4 = sorted(f for f in os.listdir(ctp) if f.lower().endswith(".mp4"))
            except Exception:
                pass
            if mp4:
                d["mp4"] = mp4
            out["packs"][seg + "|" + tp + "|" + ct] = d
sd = os.path.join(root, "_slepki_data")
for folder in (os.listdir(sd) if os.path.isdir(sd) else []):
    if folder.startswith("corpus"):          # большие корпуса не нужны для создания РК
        continue
    fp = os.path.join(sd, folder)
    if not os.path.isdir(fp):
        continue
    rec = {}
    try:
        rec["videos"] = sorted(f for f in os.listdir(os.path.join(fp, "videos")) if f.lower().endswith(".mp4"))
    except Exception:
        pass
    try:
        rec["videos_map"] = json.load(open(os.path.join(fp, "_videos_map.json"), encoding="utf-8"))
    except Exception:
        pass
    if rec:
        out["slepki_data"][folder] = rec
img_ext = (".png", ".jpg", ".jpeg", ".webp")
vid_ext = (".mp4", ".mov")
skip_top = {"kontent_oktyabr", "corpus", "instructions"}
external_limit = 5000
agency_root = os.path.dirname(root)
count_by_top = {}
for cur, dirs, files in os.walk(agency_root):
    rel_cur = os.path.relpath(cur, agency_root)
    parts = [] if rel_cur == "." else rel_cur.split(os.sep)
    top = parts[0] if parts else ""
    if top.startswith(".") or top in {"corpus", "corpus_krasnodar", "instructions", "data", "playbooks", "scripts"}:
        dirs[:] = []
        continue
    if top == "kontent_oktyabr":
        # Основные segment/tp/ct и _image_store уже индексируются отдельно. Здесь добавляем только
        # проектные медиа из _slepki_data/*/creatives... и videos.
        if len(parts) < 2:
            dirs[:] = [d for d in dirs if d == "_slepki_data"]
            continue
        if parts[1] != "_slepki_data":
            dirs[:] = []
            continue
        if len(parts) >= 3 and parts[2].startswith("corpus"):
            dirs[:] = []
            continue
    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules", ".git"}]
    top_key = "/".join(parts[:3]) if top == "kontent_oktyabr" and len(parts) >= 3 else (top or "root")
    base_project = top_key
    if top == "kontent_oktyabr" and len(parts) >= 3 and parts[1] == "_slepki_data":
        base_project = "_slepki_data/" + parts[2]
    for d in dirs:
        if re.fullmatch(r"ct\d{4}", d.lower()):
            # Внешние/проектные ct-папки должны быть видны в дереве даже без медиа.
            # Основной kontent_oktyabr/segment/tp/ct уже есть в out["packs"].
            if not (top == "kontent_oktyabr" and re.match(r"^[^/]+/tp\d+$", "/".join(parts))):
                out["external_folders"].setdefault("Проекты|" + base_project + "|" + d.lower(), True)
    count = count_by_top.get(top_key, 0)
    if count >= external_limit:
        dirs[:] = []
        continue
    rel_cur_l = rel_cur.lower()
    dir_ct_match = re.search(r"(?:^|/)(?:creatives[_-]?ct|ct)(\d{4})(?:/|$)", rel_cur_l)
    dir_ct = ("ct" + dir_ct_match.group(1)) if dir_ct_match else ""
    videos_map = {}
    if top == "kontent_oktyabr" and len(parts) >= 4 and parts[1] == "_slepki_data" and parts[-1] == "videos":
        try:
            videos_map = json.load(open(os.path.join(agency_root, "kontent_oktyabr", "_slepki_data", parts[2], "_videos_map.json"), encoding="utf-8"))
        except Exception:
            videos_map = {}
    for fn in files:
        low = fn.lower()
        if not low.endswith(img_ext + vid_ext):
            continue
        rel_all = os.path.relpath(os.path.join(cur, fn), agency_root)
        if rel_all.startswith("kontent_oktyabr/_image_store/"):
            continue
        # Не дублируем основной контент-пак segment/tp/ct: он уже есть в out["packs"].
        if re.match(r"^[^/]+/[^/]+/tp\d+/ct\d{4}/", rel_all) and top == "kontent_oktyabr":
            continue
        m = re.search(r"ct\d{4}", rel_all.lower())
        ct = dir_ct or (m.group(0) if m else "ct0000")
        if low.endswith(vid_ext) and videos_map:
            mapped_cts = []
            stem = os.path.splitext(fn)[0].lower()
            for mk, vals in videos_map.items():
                if fn in (vals or []):
                    key = str(mk or "").lower()
                    if re.fullmatch(r"ct\d{4}", key):
                        mapped_cts.append(key)
                    else:
                        # Когда карта хранит модельный ключ (jolion/h5), точного ct здесь нет.
                        # Оставляем ролик общим, чтобы не прикрепить его к неверной группе.
                        pass
            if mapped_cts:
                cts_for_file = sorted(set(mapped_cts))
            else:
                cts_for_file = [ct]
        else:
            cts_for_file = [ct]
        kind = "video_external" if low.endswith(vid_ext) else "image_external"
        if top == "kontent_oktyabr" and len(parts) >= 3 and parts[1] == "_slepki_data":
            project = "_slepki_data/" + parts[2]
        else:
            project = top_key
        for file_ct in cts_for_file:
            key = "Проекты|" + project + "|" + file_ct
            out["external_assets"].setdefault(key, []).append({"rel": rel_all, "kind": kind})
        count += 1
        count_by_top[top_key] = count
        if count >= external_limit:
            break
manual_root = os.path.join(os.path.dirname(os.path.dirname(root)), "creatives", "Manual")
if os.path.isdir(manual_root):
    for ct in sorted(os.listdir(manual_root)):
        ctp = os.path.join(manual_root, ct)
        ctl = ct.lower()
        if not re.fullmatch(r"ct\d{4}", ctl) or not os.path.isdir(ctp):
            continue
        key = "Manual|manual|" + ctl
        out["external_folders"].setdefault(key, True)
        for cur, dirs, files in os.walk(ctp):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
            for fn in sorted(files):
                low = fn.lower()
                if not low.endswith(img_ext + vid_ext):
                    continue
                kind = "video_external" if low.endswith(vid_ext) else "image_manual"
                out["external_assets"].setdefault(key, []).append({
                    "remote": os.path.join(cur, fn),
                    "kind": kind,
                })
# Видео per-ct: /Users/Shared/agency/Video/<ctNNNN>/<ctNNNN_NN>.mp4 — общий пул роликов,
# нарезанных по коду модели (ct = coder-ct, как фид-картинки). Индексируем так же, как Manual:
# ключ external_assets "Video|video|<ct>", kind video_external. Загрузка в РК — по ct.
video_root = os.path.join(os.path.dirname(os.path.dirname(root)), "Video")
if os.path.isdir(video_root):
    for ct in sorted(os.listdir(video_root)):
        ctp = os.path.join(video_root, ct)
        ctl = ct.lower()
        if not re.fullmatch(r"ct\d{4}", ctl) or not os.path.isdir(ctp):
            continue
        key = "Video|video|" + ctl
        out["external_folders"].setdefault(key, True)
        for cur, dirs, files in os.walk(ctp):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
            for fn in sorted(files):
                if not fn.lower().endswith(vid_ext):
                    continue
                out["external_assets"].setdefault(key, []).append({
                    "remote": os.path.join(cur, fn),
                    "kind": "video_external",
                })
print(json.dumps(out, ensure_ascii=False))
'''


def refresh_index(timeout: int = 120) -> bool:
    """Перестроить индекс НА M3 (ssh, локальный диск Mac) и атомарно записать локально.
    Возвращает True при успехе. Безопасно при сбое связи (вернёт False, старый индекс цел)."""
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        r = subprocess.run(_M3_SSH + ["python3", "-", M3_PACK_ROOT],
                           input=_INDEX_BUILDER, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return False
        data = json.loads(r.stdout)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, INDEX_PATH)
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_index() -> dict:
    """Индекс из локального файла (кэш по mtime — подхватывает обновления таймера).
    Если файла нет / устарел — пытаемся построить (один раз, с таймаутом)."""
    try:
        if not os.path.isfile(INDEX_PATH) or (_time.time() - os.path.getmtime(INDEX_PATH)) > INDEX_MAX_AGE:
            refresh_index()
    except Exception:  # noqa: BLE001
        pass
    try:
        m = os.path.getmtime(INDEX_PATH)
        if _INDEX_CACHE["data"] is None or m != _INDEX_CACHE["mtime"]:
            with open(INDEX_PATH, encoding="utf-8") as f:
                _INDEX_CACHE["data"] = json.load(f)
            _INDEX_CACHE["mtime"] = m
        return _INDEX_CACHE["data"] or {}
    except Exception:  # noqa: BLE001
        return {"feeds": [], "packs": {}, "slepki_data": {}, "external_assets": {}, "external_folders": {}}


def _prune_cache() -> None:
    """LRU: если кэш байтов перерос лимит — удаляем самые старые по atime."""
    try:
        files = []
        total = 0
        for n in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_atime, st.st_size, p))
            total += st.st_size
        cap = CACHE_CAP_MB * 1024 * 1024
        if total <= cap:
            return
        for _at, sz, p in sorted(files):           # старые сначала
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
            if total <= cap * 0.9:
                break
    except Exception:  # noqa: BLE001
        pass


def _fetch_bytes(remote_abs: str) -> str | None:
    """Точечно скачать ОДИН файл с M3 по точному пути (scp+timeout) в локальный кэш.
    Возвращает локальный путь или None (при таймауте/сбое — НЕ зависает вечно)."""
    if not remote_abs:
        return None
    # ЛОКАЛЬНАЯ КОПИЯ ПЕРВОЙ: пак перенесён на Proxmox → маппим M3-путь на локальную папку.
    # Есть файл локально → отдаём его без ssh (isdir/isfile по локальному диску, НЕ по sshfs).
    if _LOCAL_MIRROR_ROOT and remote_abs.startswith(M3_AGENCY_ROOT + "/"):
        _lm = _LOCAL_MIRROR_ROOT + remote_abs[len(M3_AGENCY_ROOT):]
        try:
            if os.path.isfile(_lm) and os.path.getsize(_lm) > 0:
                return _lm
        except OSError:
            pass
    # VIDEO-POOL: agency/Video/<ct>/*.mp4 синкается компрессором в _video_pool/<ct>/*.mp4.
    # Возвращаем локальный путь ТОЛЬКО если файл существует и не пустой (лёгкий чек, без ffprobe —
    # валидность кодека гарантирована компрессором). Нет в пуле → провалиться в ssh-фетч ниже.
    if _LOCAL_MIRROR_ROOT and remote_abs.startswith(M3_VIDEO_ROOT + "/"):
        _lm = os.path.join(_LOCAL_MIRROR_ROOT, "_video_pool") + remote_abs[len(M3_VIDEO_ROOT):]
        try:
            if os.path.isfile(_lm) and os.path.getsize(_lm) > 0:
                return _lm
        except OSError:
            pass
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        h = hashlib.md5(remote_abs.encode("utf-8")).hexdigest()
        ext = posixpath.splitext(remote_abs)[1].lower()
        local = os.path.join(CACHE_DIR, h + ext)
        if os.path.isfile(local) and os.path.getsize(local) > 0:
            try:
                os.utime(local, None)               # LRU-touch
            except OSError:
                pass
            return local
        # ssh cat (НЕ scp: современный scp на SFTP-бэкенде ломает кириллический путь
        # «нейродиректолог» → No such file; ssh+remote-shell кириллицу обрабатывает корректно).
        r = subprocess.run(_M3_SSH + ["cat", shlex.quote(remote_abs)],
                           capture_output=True, timeout=_FETCH_TIMEOUT)
        if r.returncode == 0 and r.stdout:
            tmp = local + ".part"
            with open(tmp, "wb") as fh:
                fh.write(r.stdout)
            os.replace(tmp, local)
            if os.path.getsize(local) > 0:
                _prune_cache()
                return local
        if os.path.isfile(local):
            try:
                os.remove(local)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_remote_asset(remote_abs: str) -> str | None:
    """Публичная обёртка для ленивого превью/правил контента: remote M3 path → local cache path."""
    return _fetch_bytes(remote_abs)


def fetch_remote_thumbnail(remote_abs: str, size: int = 360) -> str | None:
    """Скачать/создать маленькую JPEG-миниатюру для вкладки «Контент».

    M3 для нас read-only: не создаём временные файлы на Mac. Сначала тянем оригинал
    в локальный кэш сервера, затем по возможности делаем локальную миниатюру.
    """
    if not remote_abs:
        return None
    try:
        os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
        size = max(120, min(int(size or 360), 720))
        h = hashlib.md5(f"{size}:{remote_abs}".encode("utf-8")).hexdigest()
        local = os.path.join(THUMB_CACHE_DIR, h + ".jpg")
        if os.path.isfile(local) and os.path.getsize(local) > 0:
            try:
                os.utime(local, None)
            except OSError:
                pass
            return local
        src = _fetch_bytes(remote_abs)
        if not src:
            return None
        if _make_local_thumbnail(src, local, size):
            return local
        return src
    except Exception:  # noqa: BLE001
        pass
    return None


def _image_cli() -> str | None:
    for cmd in ("magick", "convert"):
        p = shutil.which(cmd)
        if p:
            return p
    return None


def _make_local_thumbnail(src: str, dst: str, size: int) -> bool:
    cli = _image_cli()
    if not cli:
        return False
    part = dst + ".part"
    cmd = ([cli, src, "-auto-orient", "-thumbnail", f"{int(size)}x{int(size)}>", "-quality", "82", part]
           if os.path.basename(cli) != "magick" else
           [cli, src, "-auto-orient", "-thumbnail", f"{int(size)}x{int(size)}>", "-quality", "82", part])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and os.path.isfile(part) and os.path.getsize(part) > 0:
            os.replace(part, dst)
            return os.path.getsize(dst) > 0
    except Exception:  # noqa: BLE001
        pass
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError:
        pass
    return False


def prefetch_remote_thumbnails(remote_paths: list[str], size: int = 360, max_workers: int = 4) -> None:
    """Прогреть cache миниатюр для пачки ассетов."""
    paths = [p for p in remote_paths if p]
    if not paths:
        return
    if len(paths) == 1:
        fetch_remote_thumbnail(paths[0], size=size)
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(paths)))) as ex:
        list(ex.map(lambda p: fetch_remote_thumbnail(p, size=size), paths))


def m3_manual_write_probe(timeout: int = 8) -> bool:
    """M3 считается read-only: запись туда не проверяем и не используем."""
    return False


def _fetch_many(remote_paths: list) -> dict:
    """Параллельно тянет список файлов с M3 (8 потоков через один mux-канал) → {remote: local|None}.
    Сильно ускоряет image-heavy кампании (сотни картинок tp1): вместо последовательных ~Nс — ~N/8."""
    paths = [p for p in remote_paths if p]
    if not paths:
        return {}
    if len(paths) == 1:
        return {paths[0]: _fetch_bytes(paths[0])}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(paths))) as ex:
        locals_ = list(ex.map(_fetch_bytes, paths))
    return dict(zip(paths, locals_))


def _remote_ct_dir(segment: str, tp: str, ct: str) -> str:
    return posixpath.join(M3_PACK_ROOT, segment, tp, _norm_ct(ct) or GENERAL_CT)


def _pack_entry(segment: str, tp: str, ct: str) -> dict:
    return _load_index().get("packs", {}).get(f"{segment}|{tp}|{_norm_ct(ct) or GENERAL_CT}", {})


def remote_asset_key(remote_abs: str) -> str:
    """Стабильный ключ ассета: совпадает с именем файла в локальном кэше без расширения."""
    return hashlib.md5((remote_abs or "").encode("utf-8")).hexdigest()


def _png_gray_bytes(data: bytes) -> list[int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not png")
    pos = 8
    width = height = bit_depth = color_type = None
    comp: list[bytes] = []
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            comp.append(chunk)
        elif typ == b"IEND":
            break
    if not width or not height or bit_depth != 8 or color_type not in (2, 6):
        raise ValueError("unsupported png")
    channels = 3 if color_type == 2 else 4
    raw = zlib.decompress(b"".join(comp))
    stride = width * channels
    prev = [0] * stride
    out: list[int] = []
    p = 0
    for _ in range(height):
        filt = raw[p]
        p += 1
        cur = list(raw[p:p + stride])
        p += stride
        recon = [0] * stride
        for i, x in enumerate(cur):
            left = recon[i - channels] if i >= channels else 0
            up = prev[i]
            ul = prev[i - channels] if i >= channels else 0
            if filt == 0:
                val = x
            elif filt == 1:
                val = x + left
            elif filt == 2:
                val = x + up
            elif filt == 3:
                val = x + ((left + up) // 2)
            elif filt == 4:
                pr = left + up - ul
                pa, pb, pc = abs(pr - left), abs(pr - up), abs(pr - ul)
                val = x + (left if pa <= pb and pa <= pc else up if pb <= pc else ul)
            else:
                raise ValueError("bad png filter")
            recon[i] = val & 255
        for i in range(0, len(recon), channels):
            r, g, b = recon[i], recon[i + 1], recon[i + 2]
            out.append((r * 299 + g * 587 + b * 114) // 1000)
        prev = recon
    return out


def _remote_asset_is_allowed(remote_abs: str) -> bool:
    remote_abs = remote_abs or ""
    roots = (M3_AGENCY_ROOT + "/", M3_MANUAL_ROOT + "/")
    return any(remote_abs.startswith(root) for root in roots)


def _local_resized_png_bytes(local_path: str, size: int) -> bytes | None:
    cli = _image_cli()
    if not cli or not local_path:
        return None
    cmd = ([cli, local_path, "-auto-orient", "-resize", f"{int(size)}x{int(size)}!",
            "-colorspace", "Gray", "png:-"]
           if os.path.basename(cli) != "magick" else
           [cli, local_path, "-auto-orient", "-resize", f"{int(size)}x{int(size)}!",
            "-colorspace", "Gray", "png:-"])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:  # noqa: BLE001
        pass
    return None


def remote_asset_ahash(remote_abs: str, size: int = 16) -> int | None:
    """Визуальный aHash для ассета на M3.

    M3 read-only: исходник скачиваем в локальный кэш сервера и считаем хеш локально.
    Если локального конвертера нет — возвращаем None, чтобы ассет не пропал из UI.
    """
    remote_abs = remote_abs or ""
    key = f"{size}:{remote_abs}"
    if key in _REMOTE_AHASH_CACHE:
        return _REMOTE_AHASH_CACHE[key]
    if not _remote_asset_is_allowed(remote_abs):
        _REMOTE_AHASH_CACHE[key] = None
        return None
    try:
        src = _fetch_bytes(remote_abs)
        png = _local_resized_png_bytes(src or "", size)
        if not png:
            _REMOTE_AHASH_CACHE[key] = None
            return None
        vals = _png_gray_bytes(png)
        avg = sum(vals) / max(1, len(vals))
        bits = 0
        for v in vals:
            bits = (bits << 1) | (1 if v >= avg else 0)
        _REMOTE_AHASH_CACHE[key] = bits
        return bits
    except Exception:  # noqa: BLE001
        _REMOTE_AHASH_CACHE[key] = None
        return None


def remote_asset_phash(remote_abs: str, size: int = 32, low: int = 8) -> int | None:
    """DCT pHash для похожих рекламных макетов.

    Для вкладки «Контент» pHash полезнее aHash: он лучше схлопывает одинаковые
    композиции с небольшими различиями в тексте/цвете. Ошибка декодирования не
    считается ошибкой ассета — вернём None, и UI покажет файл как есть.
    """
    remote_abs = remote_abs or ""
    key = f"{size}:{low}:{remote_abs}"
    if key in _REMOTE_PHASH_CACHE:
        return _REMOTE_PHASH_CACHE[key]
    if not _remote_asset_is_allowed(remote_abs):
        _REMOTE_PHASH_CACHE[key] = None
        return None
    try:
        src = _fetch_bytes(remote_abs)
        png = _local_resized_png_bytes(src or "", size)
        if not png:
            _REMOTE_PHASH_CACHE[key] = None
            return None
        vals = _png_gray_bytes(png)
        if len(vals) != size * size:
            _REMOTE_PHASH_CACHE[key] = None
            return None
        cos = _PHASH_COS_CACHE.get(size)
        if cos is None:
            cos = [[math.cos((2 * x + 1) * u * math.pi / (2 * size)) for x in range(size)] for u in range(low)]
            _PHASH_COS_CACHE[size] = cos
        coeff: list[float] = []
        for u in range(low):
            for v in range(low):
                if u == 0 and v == 0:
                    continue
                s = 0.0
                for y in range(size):
                    row = y * size
                    cv = cos[v][y]
                    for x in range(size):
                        s += vals[row + x] * cos[u][x] * cv
                coeff.append(s)
        if not coeff:
            _REMOTE_PHASH_CACHE[key] = None
            return None
        med = sorted(coeff)[len(coeff) // 2]
        bits = 0
        for c in coeff:
            bits = (bits << 1) | (1 if c >= med else 0)
        _REMOTE_PHASH_CACHE[key] = bits
        return bits
    except Exception:  # noqa: BLE001
        _REMOTE_PHASH_CACHE[key] = None
        return None


def encode_remote_asset(remote_abs: str) -> str:
    return base64.urlsafe_b64encode((remote_abs or "").encode("utf-8")).decode("ascii").rstrip("=")


def decode_remote_asset(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


SLEPOK_LABELS = {
    "common": "Общее",
    "pavlov": "Слепок Павлов",
    "scherbakova": "Слепок Щербакова",
    "kryuchkova": "Слепок Крючкова",
    "terehov": "Слепок Терехов",
    "karavaev": "Слепок Караваев",
    "salamahin": "Слепок Саламахин",
    "gordeeva": "Слепок Гордеева",
    "zubakin": "Слепок Зубакин",
    "chepelev": "Слепок Чепелев",
    "tumashenko": "Слепок Тумашенко",
    "kuderko": "Слепок Кудерко",
}


def _slepok_label(slepok: str) -> str:
    key = (slepok or "common").strip().lower() or "common"
    return SLEPOK_LABELS.get(key, key)


def _slepok_tags(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in str(raw or "").split(",") if p.strip()]
    return parts or ["common"]


def content_tree(force_refresh: bool = False) -> dict:
    """Лёгкое дерево M3-индекса без байтов: slepok → segment → tp → ct.
    Используется вкладкой «Контент». Превью грузятся отдельным ленивым endpoint."""
    if force_refresh:
        refresh_index()
    idx = _load_index()
    try:
        mtime = os.path.getmtime(INDEX_PATH)
    except OSError:
        mtime = -1
    if not force_refresh and _CONTENT_TREE_CACHE["data"] is not None and _CONTENT_TREE_CACHE["mtime"] == mtime:
        return _CONTENT_TREE_CACHE["data"]
    tree: dict[str, dict[str, dict[str, int]]] = {}
    slepok_tree: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for key, entry in (idx.get("packs") or {}).items():
        try:
            segment, tp, ct = key.split("|", 2)
        except ValueError:
            continue
        n = sum(len(entry.get(k) or []) for k in ("image", "image_slepki", "video", "mp4", "direct_image"))
        tree.setdefault(segment, {}).setdefault(tp, {})[ct] = n
        counts: dict[str, int] = {}
        common_n = sum(len(entry.get(k) or []) for k in ("image", "video", "mp4", "direct_image"))
        if common_n:
            counts["common"] = common_n
        for ln in entry.get("image_slepki", []) or []:
            _rel, _sep, slp = str(ln).partition("\t")
            for tag in _slepok_tags(slp):
                counts[tag] = counts.get(tag, 0) + 1
        if not counts:
            counts["common"] = 0
        for slp, cnt in counts.items():
            slepok_tree.setdefault(slp, {}).setdefault(segment, {}).setdefault(tp, {})[ct] = cnt
    for key in (idx.get("external_folders") or {}):
        try:
            segment, tp, ct = key.split("|", 2)
        except ValueError:
            continue
        if segment == "Проекты":
            continue
        tree.setdefault(segment, {}).setdefault(tp, {}).setdefault(ct, 0)
        slepok_tree.setdefault("common", {}).setdefault(segment, {}).setdefault(tp, {}).setdefault(ct, 0)
    for key, items in (idx.get("external_assets") or {}).items():
        try:
            segment, tp, ct = key.split("|", 2)
        except ValueError:
            continue
        if segment == "Проекты":
            continue
        n = len(items or [])
        if not n:
            continue
        tree.setdefault(segment, {}).setdefault(tp, {})[ct] = tree.setdefault(segment, {}).setdefault(tp, {}).get(ct, 0) + n
        # Внешние проектные папки не привязаны к конкретному слепку. Показываем в «Общее»,
        # а применимость к другим слепкам пользователь задаёт через allowed_slepki.
        slepok_tree.setdefault("common", {}).setdefault(segment, {}).setdefault(tp, {})[ct] = (
            slepok_tree.setdefault("common", {}).setdefault(segment, {}).setdefault(tp, {}).get(ct, 0) + n
        )
    out = []
    for segment in sorted(tree):
        tps = []
        for tp in sorted(tree[segment]):
            cts = [{"ct": ct, "assets": tree[segment][tp][ct]} for ct in sorted(tree[segment][tp])]
            tps.append({"tp": tp, "cts": cts, "assets": sum(x["assets"] for x in cts)})
        out.append({"segment": segment, "tps": tps, "assets": sum(t["assets"] for t in tps)})
    slepki = []
    order = {"common": 0, "pavlov": 1, "scherbakova": 2, "kryuchkova": 3, "terehov": 4, "karavaev": 5,
              "salamahin": 6, "gordeeva": 7, "zubakin": 8, "chepelev": 9, "tumashenko": 10,
              "kuderko": 11}
    for slp in sorted(slepok_tree, key=lambda x: (order.get(x, 99), x)):
        segments = []
        for segment in sorted(slepok_tree[slp]):
            tps = []
            for tp in sorted(slepok_tree[slp][segment]):
                cts = [{"ct": ct, "assets": slepok_tree[slp][segment][tp][ct]}
                       for ct in sorted(slepok_tree[slp][segment][tp])]
                tps.append({"tp": tp, "cts": cts, "assets": sum(x["assets"] for x in cts)})
            segments.append({"segment": segment, "tps": tps, "assets": sum(t["assets"] for t in tps)})
        slepki.append({"slepok": slp, "label": _slepok_label(slp),
                       "segments": segments, "assets": sum(s["assets"] for s in segments)})
    data = {"slepki": slepki, "segments": out, "feeds": len(idx.get("feeds") or [])}
    _CONTENT_TREE_CACHE["mtime"] = mtime
    _CONTENT_TREE_CACHE["data"] = data
    return data


def content_assets(segment: str, tp: str, ct: str, slepok: str = "") -> list[dict]:
    """Ассеты одной ct-папки с remote path/token. Байты не скачивает."""
    base = _remote_ct_dir(segment, tp, ct)
    entry = _pack_entry(segment, tp, ct)
    rows: list[dict] = []
    wanted = (slepok or "").strip().lower()

    def add(kind: str, rel: str, slepok: str = "") -> None:
        tags = [t for t in _slepok_tags(slepok) if t != "common"]
        if wanted:
            if wanted == "common":
                if tags:
                    return
            elif wanted not in tags:
                return
        rel = (rel or "").split("\t")[0].strip()
        if not rel:
            return
        display_slepok = wanted if wanted and wanted != "common" else (tags[0] if tags else "")
        remote = posixpath.normpath(posixpath.join(base, rel))
        rows.append({
            "asset_key": remote_asset_key(remote),
            "asset_type": kind,
            "name": posixpath.basename(remote),
            "remote": remote,
            "token": encode_remote_asset(remote),
            "slepok": display_slepok,
            "slepok_tags": tags,
            "slepok_label": _slepok_label(display_slepok or "common"),
        })

    for ln in entry.get("image_slepki", []) or []:
        rel, _, slp = str(ln).partition("\t")
        add("image_slepki", rel, slp.strip())
    for ln in entry.get("image", []) or []:
        add("image", str(ln))
    for ln in entry.get("video", []) or []:
        add("video", str(ln))
    for fn in entry.get("mp4", []) or []:
        add("video", str(fn))
    for fn in entry.get("direct_image", []) or []:
        add("image_direct", str(fn))
    ext_key = f"{segment}|{tp}|{_norm_ct(ct) or GENERAL_CT}"
    for rec in (_load_index().get("external_assets") or {}).get(ext_key, []) or []:
        remote = str(rec.get("remote") or "").strip()
        if not remote:
            rel = str(rec.get("rel") or "").strip()
            if not rel:
                continue
            remote = posixpath.normpath(posixpath.join(M3_AGENCY_ROOT, rel))
        kind = str(rec.get("kind") or "image_external")
        rows.append({
            "asset_key": remote_asset_key(remote),
            "asset_type": kind,
            "name": posixpath.basename(remote),
            "remote": remote,
            "token": encode_remote_asset(remote),
            "slepok": "",
            "slepok_tags": [],
            "slepok_label": _slepok_label("common"),
        })
    return rows

SEGMENTS = ("Монобренд", "Мультибренд", "Квиз", "Мульти + БУ", "С пробегом")
SLEPOK_KEYS = ("pavlov", "scherbakova", "kryuchkova", "terehov", "karavaev",
               "salamahin", "gordeeva", "zubakin", "chepelev", "tumashenko",
               "kuderko")
# БАГ-7: слепки директологов, у которых ЕСТЬ б/у-сайты («С пробегом», «Мульти+БУ»).
# При создании РК для НЕ-б/у-сайта (Мультибренд/Монобренд/Квиз) картинки этих слепков
# могут содержать б/у-авто — исключаем их через exclude_bu_slepoks=True.
_BU_SLEPOKS: frozenset = frozenset({"terehov", "gordeeva"})
GENERAL_CT = "ct0000"           # «полное отсутствие ключей» — общий мультибренд

_CT_RE = re.compile(r"^ct\d{4}$")


def _norm_ct(ct: str | None) -> str:
    """Нормализует код к виду ctNNNN (берём первый 4-значный ct из строки)."""
    s = (ct or "").strip().lower()
    if _CT_RE.match(s):
        return s
    m = re.search(r"ct\d{4}", s)
    return m.group(0) if m else ""


def _read_lines(path: str) -> list:
    """Чтение текстового файла пака. Если путь под sshfs-монтом — читаем через
    `timeout cat` (FUSE-чтение прерываемо → процесс убивается по таймауту, НЕ виснет вечно).
    Иначе (локальный кэш/индекс) — обычный open."""
    try:
        if path.startswith(PACK_MOUNT + os.sep):
            r = subprocess.run(["timeout", "12", "cat", path], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return []
            src = r.stdout.splitlines()
        else:
            with open(path, encoding="utf-8") as f:
                src = list(f)
        return [ln.strip() for ln in src if ln.strip() and not ln.lstrip().startswith("#")]
    except Exception:  # noqa: BLE001
        return []


def _dedup(seq) -> list:
    seen, out = set(), []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# БАГ-5: фильтр картинок с сайтов б/у авто по URL/пути.
# Паттерны: «bu», «б.у», «bу» (кириллица у), «used», «с-пробегом», «probeg», «с_пробегом» и т.п.
_BU_IMG_RE = re.compile(
    r"(?i)[/\\_\-](bu|bу|б[_\-\.]?у|used|probeg|s[_\-]probegom|sprobeg)[/\\_\-\.]",
)


def _filter_bu_images(paths: list) -> list:
    """Исключить картинки с путей/URL, характерных для сайтов б/у авто (БАГ-5).
    Проверяется каждый сегмент пути — «bu», «used», «probeg» и аналоги между разделителями."""
    return [p for p in paths if not _BU_IMG_RE.search(str(p))]


def _ct_dir(segment: str, tp: str, ct: str) -> str:
    return os.path.join(PACK_ROOT, segment, tp, _norm_ct(ct) or GENERAL_CT)


# ── чтение контента по НАШЕМУ ct (= имя папки пака) ──────────────────────────
def read_keywords(segment: str, tp: str, ct: str, slepok: str) -> dict:
    """Ключи слепка для (сегмент, tp, наш ct) из пака.
    → {"positive":[...], "minus":[...]}. ct пустой/нет папки → пусто (caller фолбэчит)."""
    kd = os.path.join(_ct_dir(segment, tp, ct), "keywords")
    pos = _read_lines(os.path.join(kd, f"{slepok}.txt"))
    neg = (_read_lines(os.path.join(kd, f"{slepok}_minus.txt"))
           + _read_lines(os.path.join(kd, f"{slepok}_minus_shared.txt")))
    return {"positive": _dedup(pos), "minus": _dedup(neg)}


def read_callouts(segment: str, tp: str, ct: str, slepok: str) -> list:
    """Уточнения слепка для (сегмент, tp, наш ct) — список строк."""
    p = os.path.join(_ct_dir(segment, tp, ct), "callouts", f"{slepok}.txt")
    return _dedup(_read_lines(p))


def read_images(segment: str, tp: str, ct: str) -> list:
    """Фид-картинки для (сегмент, tp, наш ct) → ЛОКАЛЬНЫЕ пути (точечно скачаны с M3).
    Манифест ``image.txt`` берётся из локального индекса; байты — scp+timeout в кэш.
    БАГ-5: пути б/у-сайтов (bu/used/probeg) фильтруются через _filter_bu_images."""
    base = _remote_ct_dir(segment, tp, ct)
    rels = []
    for rel in _pack_entry(segment, tp, ct).get("image", []):
        rel = rel.split("\t")[0].strip()
        if rel:
            rels.append(posixpath.normpath(posixpath.join(base, rel)))
    rels = _filter_bu_images(rels)                         # БАГ-5: убрать б/у-пути до скачивания
    got = _fetch_many(rels)                                # параллельно
    return _dedup([got[r] for r in rels if got.get(r)])


def read_slepok_images(segment: str, tp: str, ct: str, slepok: str) -> list:
    """Слепок-картинки для (сегмент, tp, наш ct, slepok) из манифеста ``image_slepki.txt``.
    Это ИСТОЧНИК картинок для РСЯ-объявлений (tp1): строки ``<relpath>\\t<slepok>`` ведут в
    ``_image_store/slepki/``. Возвращаем ЛОКАЛЬНЫЕ пути ТОЛЬКО нужного slepok (или без тега).
    Структура — из локального индекса, байты — точечный scp+timeout (без sshfs-обхода).
    БАГ-5: пути б/у-сайтов (bu/used/probeg) фильтруются через _filter_bu_images."""
    base = _remote_ct_dir(segment, tp, ct)
    rels = []
    for ln in _pack_entry(segment, tp, ct).get("image_slepki", []):
        rel, _, slp = ln.partition("\t")
        rel = rel.strip()
        slp = slp.strip()
        if not rel:
            continue
        if slepok and slp and slepok not in _slepok_tags(slp):
            continue                                       # картинка другого слепка — пропускаем
        rels.append(posixpath.normpath(posixpath.join(base, rel)))
    rels = _filter_bu_images(rels)                         # БАГ-5: убрать б/у-пути до скачивания
    got = _fetch_many(rels)                                # параллельно
    return _dedup([got[r] for r in rels if got.get(r)])


def read_any_slepok_images(segment: str, tp: str, ct: str, prefer: str = "",
                           exclude_bu_slepoks: bool = False) -> list:
    """Ищет картинки для ct во всех слепках SLEPOK_KEYS.
    prefer — попробовать первым (текущий слепок кампании).
    exclude_bu_slepoks=True — пропускать слепки из _BU_SLEPOKS (Терехов и др.) для НЕ-б/у-сайтов:
    их картинки могут содержать б/у-авто (хэшированные имена — _BU_IMG_RE не спасает).
    Фолбэк — глобальные фид-картинки для того же ct.
    ЗАПРЕЩЕНО менять ct: ct0000 и другие марки не используются."""
    keys = ([prefer] + [k for k in SLEPOK_KEYS if k != prefer]) if prefer else list(SLEPOK_KEYS)
    if exclude_bu_slepoks:
        # prefer-слепок оставляем даже если он в _BU_SLEPOKS: caller запросил именно его
        keys = [k for k in keys if k == prefer or k not in _BU_SLEPOKS]
    for key in keys:
        imgs = read_slepok_images(segment, tp, ct, key)
        if imgs:
            return imgs
    return read_images(segment, tp, ct)


def read_videos(segment: str, tp: str, ct: str) -> list:
    """Видео для (сегмент, tp, наш ct) → ЛОКАЛЬНЫЕ пути .mp4 (точечно с M3).
    Источник: манифест ``video.txt`` + *.mp4 в ct-папке (оба из локального индекса)."""
    base = _remote_ct_dir(segment, tp, ct)
    entry = _pack_entry(segment, tp, ct)
    rels: list = []
    for rel in entry.get("video", []):
        rel = rel.split("\t")[0].strip()
        if rel:
            rels.append(posixpath.normpath(posixpath.join(base, rel)))
    for fn in entry.get("mp4", []):                        # *.mp4 прямо в ct-папке
        rels.append(posixpath.join(base, fn))
    got = _fetch_many(rels)                                # параллельно
    return _dedup([got[r] for r in rels if got.get(r)])[:2]   # лимит Директа — 2 видео на мастер


SLEPKI_DATA_ROOT = os.path.join(PACK_ROOT, "_slepki_data")


def videos_for_login(login: str, limit: int = 2) -> list:
    """Видео слепок-сборки аккаунта (Мастер/Товарка) с M3.

    Маппинг (из _OTCHET.md, проверено 2026-06-22): папка ``_slepki_data/<бренд>_<город>_<хэш>``
    заканчивается на СУФФИКС логина (последний сегмент после ``-``):
    ``porg-si7rw3ua`` → ``haval_ufa_si7rw3ua`` → ``videos/<Модель>.mp4``.
    Лимит Директа — 2 видео на мастер. Пусто, если у аккаунта нет слепок-сборки/видео."""
    suffix = (login or "").rsplit("-", 1)[-1].strip()
    if not suffix:
        return []
    sd = _load_index().get("slepki_data", {})
    for folder in sorted(sd):
        if folder.endswith(suffix):
            rels = [posixpath.join(M3_PACK_ROOT, "_slepki_data", folder, "videos", fn)
                    for fn in sd[folder].get("videos", [])[:limit]]
            got = _fetch_many(rels)
            return [got[r] for r in rels if got.get(r)]
    return []


def videos_for_ct(login: str, ct: str, limit: int = 2) -> list:
    """Видео ПО КОНКРЕТНОЙ МОДЕЛИ/МАРКЕ (ct) из слепок-сборки аккаунта (per-кодер).

    Механика: находим папку _slepki_data/<бренд>_<город>_<суффикс> по суффиксу логина;
    читаем _videos_map.json {<ключ_модели>: [<файл>.mp4, ...]}; имя модели из ct берём
    через ``feeds_ct_model()`` (self-index фид-картинок: ctNNNN → 'Brand Model'), затем
    берём последнее слово (модель без марки, lower) как ключ в карте.

    Пример: ct0119 → 'Haval Jolion' → ключ 'jolion' → ['Jolion.mp4', 'Jolion_1.mp4'] →
    /opt/neuro_kontent/kontent_oktyabr/_slepki_data/haval_ufa_si7rw3ua/videos/Jolion.mp4

    Фолбэк (2026-07-02): если у слепка нет ролика для модели — берём видео из общего per-ct
    пула M3 ``/Users/Shared/agency/Video/<ct>/`` через ``videos_pool_for_ct`` (так же, как
    картинки из Manual). Лимит Директа — 2 видео на мастер."""
    ct = _norm_ct(ct)
    if not ct or ct == GENERAL_CT:
        return []
    suffix = (login or "").rsplit("-", 1)[-1].strip()
    model_name = feeds_ct_model().get(ct, "")           # ct0119 → 'Haval Jolion'
    if suffix and model_name:
        # Ключ в _videos_map: модель без марки и маркетингового хвоста
        # ('Haval Jolion Новый' -> 'jolion'). Производные модели не подменяем:
        # F7X и Dargo X должны иметь отдельные видео-ключи.
        import unicodedata as _ud
        tokens = [
            _ud.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii").lower()
            if re.search(r"[A-Za-z]", t)
            else "".join(ch for ch in _ud.normalize("NFKD", t).lower() if not _ud.combining(ch))
            for t in model_name.strip().split()
        ]
        tail_noise = {"новый", "новыи", "новая", "новое", "novyi", "novy", "new"}
        tokens = [t for t in tokens if t and t not in tail_noise]
        model_key = tokens[-1] if tokens else ""
        sd = _load_index().get("slepki_data", {})
        for folder in sorted(sd):
            if folder.endswith(suffix):
                vmap = sd[folder].get("videos_map") or {}
                filenames = vmap.get(model_key, [])
                if filenames:
                    # Кандидатов берём БОЛЬШЕ чем limit (до _VIDEO_CANDIDATE_CAP) — иначе первые
                    # limit битых/коротких роликов навсегда закрывают доступ к валидным дальше.
                    cap = max(limit, min(len(filenames), _VIDEO_CANDIDATE_CAP))
                    rels = [posixpath.join(M3_PACK_ROOT, "_slepki_data", folder, "videos", fn)
                            for fn in filenames[:cap]]
                    got = _fetch_many(rels)
                    slepki = _filter_valid_videos([got[r] for r in rels if got.get(r)])[:limit]
                    if slepki:
                        return slepki
                break                                   # слепок найден, но ролика нет → пул по ct
    return videos_pool_for_ct(ct, limit)


_VIDEO_DURATION_CACHE: dict[tuple[str, float, int], float] = {}


def _ffprobe_duration(path: str) -> float:
    """Длительность видео в секундах (0.0 при сбое) — как в sync_content_m3._ffprobe_duration.
    Кэш по (путь, mtime, size): подмена файла меняет ключ, повторный выбор того же файла — бесплатный."""
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    key = (path, st.st_mtime, st.st_size)
    cached = _VIDEO_DURATION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        )
        dur = float((out.stdout or "0").strip() or 0.0)
    except Exception:  # noqa: BLE001
        dur = 0.0
    _VIDEO_DURATION_CACHE[key] = dur
    return dur


def _filter_valid_videos(paths: list) -> list:
    """Фильтр: exists + 0 < size ≤ YANDEX_VIDEO_MAX + длительность ≥ YANDEX_VIDEO_MIN_DURATION.

    Живой кейс 06.07.2026: ct0024_01/02.mp4 (3.1с) проходили size-фильтр, но Яндекс отклонял
    upload HTTP 400 (короче 5с) — а валидные ролики того же пула (_03…_06, 5.8-8.6с) не
    выбирались вовсе, т.к. отбор шёл ДО фильтрации (см. videos_pool_for_ct/videos_for_ct).
    ffprobe(0.0) при сбое чтения НЕ бракует файл (fail-open) — size-чек уже отсеял пустышки."""
    out = []
    for p in paths:
        try:
            sz = os.path.getsize(p)
            if not (os.path.isfile(p) and 0 < sz <= YANDEX_VIDEO_MAX):
                continue
            dur = _ffprobe_duration(p)
            if dur and dur < YANDEX_VIDEO_MIN_DURATION:
                continue
            out.append(p)
        except OSError:
            pass
    return out


def videos_pool_for_ct(ct: str, limit: int = 2) -> list:
    """Видео из общего per-ct пула M3 ``/Users/Shared/agency/Video/<ct>/`` (индекс
    external_assets, ключ ``Video|video|<ct>``, kind ``video_external``).

    Account-agnostic: ролики нарезаны по коду модели (ct = coder-ct, как фид-картинки),
    подходят любому аккаунту с этой моделью. Возвращает ЛОКАЛЬНЫЕ пути (точечный fetch с M3).
    Лимит Директа — 2 видео на мастер."""
    ct = _norm_ct(ct)
    if not ct or ct == GENERAL_CT:
        return []
    _lim = max(1, int(limit or 2))
    ext_assets = (_load_index().get("external_assets") or {})
    rows = ext_assets.get("Video|video|" + ct, []) or []
    rels = [str(r.get("remote") or "").strip() for r in rows
            if str(r.get("kind") or "") == "video_external"]
    # Кандидатов больше чем _lim (до _VIDEO_CANDIDATE_CAP) — фильтр (размер+длительность) идёт
    # ПОСЛЕ отбора, иначе первые _lim битых/коротких роликов навсегда закрывают валидные дальше.
    rels = [r for r in rels if r][:max(_lim, _VIDEO_CANDIDATE_CAP)]
    if rels:
        got = _fetch_many(rels)
        result = _filter_valid_videos([got[r] for r in rels if got.get(r)])[:_lim]
        if result:
            return result
    # Brand-fallback: точного ct нет в пуле Video/ (брендовый ct без своей папки, напр. ct0111
    # Haval). Берём ролики из модельных ct того же бренда (feeds_ct_model: ct→'Brand Model').
    ct_models = feeds_ct_model()
    my_model = ct_models.get(ct, "")
    brand_word = (my_model.strip().split()[0] if my_model else "").lower()
    if brand_word:
        brand_rels: list = []
        for key, rows2 in ext_assets.items():
            if not key.startswith("Video|video|ct"):
                continue
            other_ct = key.rsplit("|", 1)[-1]
            if other_ct == ct:
                continue
            other_model = ct_models.get(other_ct, "")
            if not other_model or other_model.strip().split()[0].lower() != brand_word:
                continue
            brand_rels.extend(
                str(r.get("remote") or "").strip()
                for r in rows2
                if str(r.get("kind") or "") == "video_external"
            )
        brand_rels = [r for r in brand_rels if r][:max(_lim, _VIDEO_CANDIDATE_CAP)]
        if brand_rels:
            got2 = _fetch_many(brand_rels)
            return _filter_valid_videos([got2[r] for r in brand_rels if got2.get(r)])[:_lim]
    return []


def _feeds_index() -> list:
    """Список имён файлов в ``_image_store/feeds`` из локального индекса."""
    return _load_index().get("feeds", [])


def feed_image_for_ct(ct: str) -> str | None:
    """Брендовая картинка КОНКРЕТНОЙ модели по её ct из ``_image_store/feeds``
    (файлы вида ``ct0020_id123_BAIC_BJ40.png``). → ЛОКАЛЬНЫЙ путь (точечно с M3) или None.
    Для контент-по-кодеру (tp6/tp7 по модели): картинка совпадает с маркой кодера."""
    ct = _norm_ct(ct)
    if not ct or ct == GENERAL_CT:
        return None
    for f in sorted(_feeds_index()):
        if f.startswith(ct + "_") and f.lower().endswith((".png", ".jpg", ".jpeg")):
            return _fetch_bytes(posixpath.join(M3_PACK_ROOT, "_image_store", "feeds", f))
    return None


def feed_images_for_segment(limit: int = 5) -> list:
    """Брендовые картинки из ``_image_store/feeds`` (когда в ct-паке tp6/tp7 картинок нет).
    Для Мультибренд-мастера — разные бренды (Lada/Haval/...). → до limit ЛОКАЛЬНЫХ путей."""
    cand = [posixpath.join(M3_PACK_ROOT, "_image_store", "feeds", f)
            for f in sorted(_feeds_index())
            if f.lower().endswith((".png", ".jpg", ".jpeg"))][:limit]
    got = _fetch_many(cand)
    return [got[r] for r in cand if got.get(r)][:limit]


def feed_images_for_brand(brand: str, limit: int = 5) -> list:
    """Картинки ТОЙ ЖЕ МАРКИ из ``_image_store/feeds`` — фолбэк, когда точного ct модели нет
    (брендовый ct типа ct0181 Lada, или модель без своего файла). ``brand`` из кодера
    ('Lada' / 'Haval Jolion' → марка = первое слово). Файлы вида ``ctNNNN_id..._Lada_Granta.png``.
    Матч по токену ``_<марка>_`` в имени файла (не подстрока модели → нет коллизий F7/F7X).
    → до ``limit`` ЛОКАЛЬНЫХ путей моделей этой марки. Пусто, если марки нет в feeds
    (тогда caller уйдёт в ``feed_images_for_segment`` — общий микс)."""
    bw = (brand or "").strip().split()
    bw = bw[0].lower() if bw else ""
    if not bw:
        return []
    out: list = []
    for f in sorted(_feeds_index()):
        if not f.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        if f"_{bw}_" in f.lower():                 # марка как отдельный токен в имени файла
            lp = _fetch_bytes(posixpath.join(M3_PACK_ROOT, "_image_store", "feeds", f))
            if lp:
                out.append(lp)
            if len(out) >= limit:
                break
    return out


def has_content(segment: str, tp: str, ct: str, slepok: str) -> bool:
    """Есть ли в паке хоть что-то для этого (сегмент, tp, ct, slepok)."""
    kw = read_keywords(segment, tp, ct, slepok)
    return bool(kw["positive"] or kw["minus"]
                or read_callouts(segment, tp, ct, slepok)
                or read_images(segment, tp, ct))


# ── БАТЧ-сбор: все ключи/минус/уточнения (segment, tp) ОДНИМ ssh-вызовом ──────
# Скрипт отрабатывает на самой M3 (её локальный диск мгновенный) и отдаёт JSON.
# ~2 с на (segment, tp) против минут пофайлового sshfs. Картинки — отдельно (sshfs).
_GATHER_PY = r'''
import sys, glob, os, json
slepok, seg, tp, root = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
base = os.path.join(root, seg, tp)
def rd(p):
    try:
        return [l.strip() for l in open(p, encoding="utf-8")
                if l.strip() and not l.lstrip().startswith("#")]
    except Exception:
        return []
out = {}
for kf in glob.glob(os.path.join(base, "*", "keywords", slepok + ".txt")):
    ct = kf.split(os.sep)[-3]
    kd = os.path.dirname(kf)
    out[ct] = {"positive": rd(kf),
               "minus": rd(os.path.join(kd, slepok + "_minus.txt"))
                        + rd(os.path.join(kd, slepok + "_minus_shared.txt")),
               "callouts": rd(os.path.join(base, ct, "callouts", slepok + ".txt")),
               "images": len(rd(os.path.join(base, ct, "image.txt")))}
print(json.dumps(out, ensure_ascii=False))
'''


def gather(slepok: str, segment: str, tp: str, timeout: float = 40.0) -> dict:
    """Все ключи/минус/уточнения для (slepok, segment, tp) одним ssh-вызовом к M3.

    → {ctNNNN: {"positive":[...], "minus":[...], "callouts":[...]}}.
    Пусто при сбое связи/таймауте (caller фолбэчит). Картинки тут НЕ берём.
    """
    remote = "python3 - " + " ".join(
        shlex.quote(a) for a in (slepok, segment, tp, M3_PACK_ROOT))
    try:
        r = subprocess.run(_M3_SSH + [remote], input=_GATHER_PY,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {}


# ── вспомогательный индекс ct→модель из feed-имён (для диагностики/валидации) ─
def feeds_ct_model() -> dict:
    """{ctNNNN: 'Brand Model'} из имён файлов ``_image_store/feeds`` (локальный индекс, без sshfs)."""
    out: dict = {}
    for f in _feeds_index():
        m = re.match(r"(ct\d{4})_id\d+_(.+?)(_v\d+)?\.(?:png|jpe?g)$", f, re.I)
        if m:
            out.setdefault(m.group(1), m.group(2).replace("_", " "))
    return out


def pack_status() -> dict:
    """Статус пака — БЕЗ обращения к sshfs (только локальный индекс, не виснет)."""
    idx = _load_index()
    ok = bool(idx.get("feeds") or idx.get("packs"))
    age = None
    try:
        if os.path.isfile(INDEX_PATH):
            age = int(_time.time() - os.path.getmtime(INDEX_PATH))
    except Exception:  # noqa: BLE001
        pass
    return {"mount": PACK_MOUNT, "source": "local-index", "index_path": INDEX_PATH,
            "index_age_sec": age, "pack_root_exists": ok,
            "feeds_models": len(feeds_ct_model()), "feeds_exists": bool(idx.get("feeds"))}


# ── CLI самотест: python -m direct.kontent_pack <segment> <tp> <ct> <slepok> ──
if __name__ == "__main__":
    import json
    import sys

    print("status:", json.dumps(pack_status(), ensure_ascii=False))
    seg = sys.argv[1] if len(sys.argv) > 1 else "Монобренд"
    tp = sys.argv[2] if len(sys.argv) > 2 else "tp2"
    ct = sys.argv[3] if len(sys.argv) > 3 else "ct0031"
    slep = sys.argv[4] if len(sys.argv) > 4 else "scherbakova"
    print(f"\n[{seg} / {tp} / {ct} ({feeds_ct_model().get(ct, '?')}) / slepok={slep}]")
    kw = read_keywords(seg, tp, ct, slep)
    print(f"keywords+: {len(kw['positive'])}  minus: {len(kw['minus'])}")
    print("  sample+:", kw["positive"][:8])
    print("  sample-:", kw["minus"][:6])
    co = read_callouts(seg, tp, ct, slep)
    print(f"callouts: {len(co)} ->", co[:6])
    im = read_images(seg, tp, ct)
    print(f"images: {len(im)} ->", [os.path.basename(p) for p in im[:4]])

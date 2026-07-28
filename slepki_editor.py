"""Редактор структуры слепков (вкладка «Структура слепков» → редактируемая).

Flask-free ядро (configure(deps) DI, как create_set_finalize_queue.py). Применяет ПРАВКИ
структуры/контента слепков СЕРИЙНО из воркера очереди (те же direct_automation_jobs), а не на
лету из web-процесса — чтобы не гонять со чтением структуры воркером при создании РК.

Поддерживаемые правки (kind в теле джобы):
  • edit_keywords    — переписать ключи (positive/minus) группы пака (ct);
  • toggle_aon_aoff  — переключить сегмент автотаргет(aon)↔КС(aoff) в targeting_profile.json
                       (+ синхронизировать ag_part2-токен в gc элементов slepki_structure.json);
  • add_ct_group     — добавить ct-группу в (слепок, тип сайта, tp);
  • remove_ct_group  — удалить ct-группу.

Гарантии (архитектура, решения Семёна):
  A. enqueue-как-джоба (персист/аудит/статус в очереди/переживает рестарт). Сериализация правок
     между собой — синтетическое пустое агентство (_CREATE_MAX_PER_AGENCY=1 на бакет "").
     ⚠️ Полный FIFO против create-джоб пул НЕ гарантирует (он параллелит РАЗНЫЕ агентства), поэтому
     РЕАЛЬНЫЙ анти-гонки-механизм здесь — АТОМАРНАЯ запись (temp+os.replace): create читает
     slepki_structure.json свежим на каждый _json() и видит либо старый, либо новый ЦЕЛЫЙ снимок,
     никогда полу-запись. targeting_profile.json кэшируется — после записи сбрасываем кэш
     (profile_invalidate).
  B. dual-write контента пака: DST (/opt/neuro_content_local — читает приложение, доступно
     следующей джобе сразу) И M3-RAW-источник (m3-relay:/Users/Shared/agency/нейродиректолог —
     правда, переживёт ночной синк sync_content_m3.py + orphan-cleanup). Пишем в ОБА.
  C. preflight (scripts/slepki_preflight.preflight_dict) на ПРЕДЛАГАЕМОМ состоянии ДО записи
     структурных правок (aon/aoff, add/remove ct) — при коллизии/пустой группе/пустом tp ОТКАЗ.
  D. бэкап+timestamp slepki_structure.json/targeting_profile.json перед записью (обратимо).
  E. аудит-лог правок (кто/когда/что) — Victory public.direct_slepki_edits + локальный jsonl.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import kontent_pack as kp
from .geo_strip import normalize_geo_lines

# preflight — из scripts/ (тот же корень пакета)
import importlib.util as _ilu

_HERE = Path(__file__).resolve().parent
# slepki_structure.json больше не монолит — структура в direct/slepki/ (см. slepki_store),
# читается _load_struct()→assemble(), пишется _write_struct()→write_directologists().
_PROFILE_PATH = _HERE / "targeting_profile.json"
_AUDIT_JSONL = _HERE / "slepki_edits_audit.jsonl"

# Сериализует правки между собой в пределах процесса (доп. к бакет-гейту "" в воркере).
_EDIT_LOCK = threading.RLock()

_EDIT_KINDS = {"edit_keywords", "save_assets", "save_minus_sets",
               "toggle_aon_aoff", "add_ct_group", "remove_ct_group", "set_name_override"}

# gc-формат (кодер группы): ct0019_aon_n000_r0000_ct001_ag011_g00
_GC_RE = re.compile(
    r"^(ct\d{4})_(aon|aoff)_(n\d{3})_(r\d{4})_(ct\d{3})_(ag\d{3})_(g\d{2})$"
)
_CT4_RE = re.compile(r"^ct\d{4}$")

# лимиты валидации ключей
_KW_MAX_LEN = 4096            # Яндекс: ключевая фраза до 4096 символов
_KW_MAX_ROWS = 5000           # разумный потолок строк на группу
# запрещённые управляющие символы в фразе (кроме пробела/таба уже нет)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ── DI (инъектится из blueprint через configure) ─────────────────────────────
def _victory_conn_rw(*a, **k):
    raise RuntimeError("slepki_editor._victory_conn_rw не инъектирован (configure)")


def _profile_invalidate() -> None:  # noqa: D401  (сброс кэшей targeting/seg после записи)
    pass


def _ct_segment(ct: str) -> str:
    return "Марки"


def _ag_part1_map() -> dict:
    return {}


def _tp67_keywords_for(*a, **k) -> tuple:
    """DI: create_set_context._tp67_keywords_for (пак → библиотека реальных UAC-ключей → tp7↦tp6).
    Без configure — фолбэка нет, читается только пак (прежнее поведение)."""
    return [], []

def _tp67_keywords_for_groups(*a, **k) -> tuple:
    """DI: create_set_context._tp67_keywords_for_groups for merged tp6/tp7 positions."""
    return [], []


def _tp67_targeting_mode(g: dict) -> str:
    """DI: create_set_context._tp67_targeting_mode. Без configure — режим неизвестен."""
    return ""


def configure(deps: dict) -> None:
    globals().update(deps)


# ── preflight loader (без циклического импорта пакета scripts) ────────────────
_PREFLIGHT = {"mod": None}


def _preflight_mod():
    if _PREFLIGHT["mod"] is None:
        path = _HERE / "scripts" / "slepki_preflight.py"
        spec = _ilu.spec_from_file_location("_slepki_preflight", str(path))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _PREFLIGHT["mod"] = mod
    return _PREFLIGHT["mod"]


# ── валидация ключей ─────────────────────────────────────────────────────────
def _clean_kw_lines(rows, *, minus: bool) -> tuple[list[str], list[str]]:
    """(очищенные_строки, ошибки). trim + дедуп(caseless) + синтаксис + длина."""
    out: list[str] = []
    seen: set = set()
    errs: list[str] = []
    for raw in (rows or []):
        s = (str(raw) if raw is not None else "").strip()
        if not s:
            continue
        if _CTRL_RE.search(s):
            errs.append(f"управляющий символ в строке: {s[:40]!r}")
            continue
        if len(s) > _KW_MAX_LEN:
            errs.append(f"строка длиннее {_KW_MAX_LEN}: {s[:40]!r}…")
            continue
        if minus:
            # минус-фраза: допускаем ведущий '-'/'!'/'+', слова из букв/цифр/дефиса, кавычки/скобки.
            body = s.lstrip("-").strip()
            if not body:
                errs.append(f"пустая минус-фраза после '-': {s!r}")
                continue
            if not re.fullmatch(r'[-!+"\[\]\w\s\.]+', body, flags=re.UNICODE):
                errs.append(f"недопустимые символы в минус-фразе: {s[:40]!r}")
                continue
        key = s.lower()
        if key in seen:
            continue                      # дедуп
        seen.add(key)
        out.append(s)
    if len(out) > _KW_MAX_ROWS:
        errs.append(f"слишком много строк ({len(out)} > {_KW_MAX_ROWS})")
    return out, errs


# ── валидация компонентов пути пака (анти-traversal) ─────────────────────────
# slepok / site_type / tp попадают в путь пак-файла как есть (`_ct_dir`, `_pack_rel*`). Раньше их
# никто не проверял: `?site_type=../../../..` уводил чтение/запись за пределы PACK_ROOT (процессы
# слепков идут без `User=` → от root). Два рубежа: СИНТАКСИЧЕСКИЙ (здесь, у самих построителей
# путей — ловит любой источник спеки, включая уже стоящую в очереди джобу) и БЕЛЫЙ СПИСОК по
# реальной структуре (`validate_scope`, зовётся роутами до постановки в очередь).
_PATH_TOKEN_BAD = ("/", "\\", "\x00")


def _safe_token(value: str, what: str) -> str:
    """Компонент пути: непустой, без сепараторов/NUL, не «.»/«..». Иначе ValueError."""
    s = (str(value) if value is not None else "").strip()
    if not s:
        raise ValueError(f"{what}: пустое значение")
    if s in (".", ".."):
        raise ValueError(f"{what}: недопустимое значение {s!r}")
    for bad in _PATH_TOKEN_BAD:
        if bad in s:
            raise ValueError(f"{what}: недопустимый символ в {s[:40]!r}")
    return s


def validate_scope(slepok: str, site_type: str = "", tp: str = "") -> str | None:
    """Белый список по РЕАЛЬНОЙ структуре. → None если ок, иначе текст ошибки.

    slepok обязан быть ключом директолога, site_type — именем его типа сайта, tp — кодом tp
    внутри этого типа. Пустые site_type/tp не проверяются (не все роуты их принимают).
    Синтаксическая проверка идёт первой — до любого обращения к структуре.
    """
    try:
        _safe_token(slepok, "slepok")
        if site_type:
            _safe_token(site_type, "site_type")
        if tp:
            _safe_token(tp, "tp")
    except ValueError as e:
        return str(e)
    struct = _load_struct(mutable=False)
    d = _find_dir(struct, slepok)
    if d is None:
        return f"неизвестный слепок: {slepok[:40]!r}"
    if site_type:
        s = _find_site(d, site_type)
        if s is None:
            return f"неизвестный тип сайта: {site_type[:40]!r}"
        if tp and _find_tp(s, tp) is None:
            return f"неизвестный tp: {tp[:40]!r}"
    return None


# ── пути пака (DST + M3) ─────────────────────────────────────────────────────
def _group_fname(fname: str, slug: str) -> str:
    """Вставить per-adgroup слаг в имя пак-файла по контракту:
    ``{slepok}.txt`` → ``{slepok}__{slug}.txt``; ``{slepok}_minus.txt`` → ``{slepok}__{slug}_minus.txt``.
    ``_minus_shared.txt`` (общий пер-ct) НЕ трогаем. Пустой slug → без изменений (легаси)."""
    if not slug or fname.endswith("_minus_shared.txt"):
        return fname
    if fname.endswith("_minus.txt"):
        return f"{fname[:-len('_minus.txt')]}__{slug}_minus.txt"
    if fname.endswith(".txt"):
        return f"{fname[:-4]}__{slug}.txt"
    return fname


def _pack_rel(site_type: str, tp: str, ct: str, fname: str, group: str = "") -> str:
    """Относительный путь файла пака внутри kontent_oktyabr (подпапка keywords).
    group непустой → имя файла с per-adgroup слагом (см. _group_fname); shared остаётся общим."""
    site_type = _safe_token(site_type, "site_type")
    tp = _safe_token(tp, "tp")
    fname = _safe_token(fname, "fname")
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    return posixpath.join(site_type, tp, ctn, "keywords", _group_fname(fname, kp._group_slug(group)))


def _pack_rel_callouts(site_type: str, tp: str, ct: str, fname: str, group: str = "") -> str:
    """Относительный путь файла уточнений (callouts) внутри kontent_oktyabr.
    Зеркалит kontent_pack.read_callouts: {site_type}/{tp}/{ct}/callouts/{slepok}[__{slug}].txt."""
    site_type = _safe_token(site_type, "site_type")
    tp = _safe_token(tp, "tp")
    fname = _safe_token(fname, "fname")
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    return posixpath.join(site_type, tp, ctn, "callouts", _group_fname(fname, kp._group_slug(group)))


def _pack_rel_minus_sets(site_type: str, slepok: str) -> str:
    """Относительный путь файла ИМЕНОВАННЫХ наборов минус-слов внутри kontent_oktyabr.
    Уровень (слепок × тип сайта): {site_type}/_minus_sets/{slepok}.json.
    НЕ per-(tp,ct) (в отличие от keywords/callouts): наборы — свойство кабинета целиком.
    Файл отдельный от {slepok}_minus_shared.txt, но с 2026-07-28 движок создания читает ОБА:
    create_set_minus._read_slepok_minus_sets заводит эти наборы в библиотеке минус-фраз аккаунта
    (negativekeywordsharedsets) и привязывает их к кампаниям tp2-tp5 (решение Семёна)."""
    site_type = _safe_token(site_type, "site_type")
    slepok = _safe_token(slepok, "slepok")
    return posixpath.join(site_type, "_minus_sets", f"{slepok}.json")


def _dst_abs(rel: str) -> str:
    return os.path.join(kp.PACK_ROOT, *rel.split("/"))


def _m3_abs(rel: str) -> str:
    return posixpath.join(kp.M3_PACK_ROOT, rel)


# ── атомарная локальная запись ───────────────────────────────────────────────
def _atomic_write_local(path: str, text: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{int(time.time()*1000)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)                 # атомарно в пределах ФС → читатель видит целое


# ── запись в M3-источник через ssh (best-effort, атомарно на удалённой стороне) ──
def _ssh_write_m3(remote_abs: str, text: str) -> tuple[bool, str]:
    rdir = posixpath.dirname(remote_abs)
    tmp = f"{remote_abs}.tmp.$$"
    # mkdir -p; записать во временный; атомарно mv
    cmd = (f"mkdir -p {shlex.quote(rdir)} && cat > {shlex.quote(tmp)} && "
           f"mv -f {shlex.quote(tmp)} {shlex.quote(remote_abs)}")
    try:
        r = subprocess.run(
            kp._M3_SSH + [cmd],
            input=text, text=True, capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            return False, (r.stderr or "")[-300:]
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


# ВЕЕРНАЯ запись одного текста во все файлы (`_ssh_write_m3_many`/`_dual_write_pack_files_same`)
# удалена 2026-07-19 вместе с веерной семантикой save_assets — она и стирала per-ct различия.


# Запись РАЗНОГО текста в разные файлы M3 одной ssh-сессией. Скрипт уходит аргументом `python3 -c`
# (мал), полезная нагрузка — через stdin (объём не упирается в ARG_MAX). Атомарность на удалённой
# стороне: временный файл рядом + os.replace (читатель create-РК видит либо старое, либо новое целое).
_M3_WRITE_MAP_PY = (
    "import io,json,os,sys\n"
    "data=json.load(sys.stdin)\n"
    "for path,text in data.items():\n"
    "    d=os.path.dirname(path)\n"
    "    if d: os.makedirs(d,exist_ok=True)\n"
    "    tmp=path+'.tmpasset'\n"
    "    f=io.open(tmp,'w',encoding='utf-8');f.write(text);f.close()\n"
    "    os.replace(tmp,path)\n"
)

_PACK_CACHE_MARKER = Path(__file__).resolve().parent / "slepki_pack_cache.marker"


def _touch_pack_cache_marker() -> None:
    """Invalidate web cached pack_facts/kw_totals after local pack writes."""
    try:
        _PACK_CACHE_MARKER.write_text(str(time.time_ns()), encoding="utf-8")
    except Exception:  # noqa: BLE001 — marker is cache invalidation only; never fail the edit job
        pass


def _ssh_write_m3_map(mapping: dict) -> tuple[bool, str]:
    """Записать {абсолютный_путь_M3: текст} за ОДНУ ssh-сессию. Пустая карта → no-op."""
    if not mapping:
        return True, ""
    remote = "python3 -c " + shlex.quote(_M3_WRITE_MAP_PY)
    try:
        r = subprocess.run(
            kp._M3_SSH + [remote],
            input=json.dumps(mapping, ensure_ascii=False), text=True,
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            return False, (r.stderr or "")[-300:]
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _dual_write_pack_files_map(mapping: dict) -> dict:
    """Записать {rel: text} (у каждого файла СВОЙ текст) в DST (локально, атомарно) + M3 (батч-ssh).
    Отличие от `_dual_write_pack_files_same`: тот веером льёт один текст во все файлы и потому
    стирает per-ct различия; этот пишет ровно то, что посчитано для конкретного (tp,ct)."""
    dst_fail: list[dict] = []
    for rel, text in mapping.items():
        try:
            _atomic_write_local(_dst_abs(rel), text)
        except Exception as e:  # noqa: BLE001
            dst_fail.append({"rel": rel, "error": str(e)[:120]})
    if mapping and not dst_fail:
        _touch_pack_cache_marker()
    m3_ok, m3_err = _ssh_write_m3_map({_m3_abs(r): t for r, t in mapping.items()})
    return {"ok": (not dst_fail) and m3_ok,
            "count": len(mapping), "dst_ok": len(mapping) - len(dst_fail),
            "m3_ok": m3_ok, "m3_error": (m3_err or None), "dst_fail": dst_fail[:20]}


def _dual_write_pack_file(rel: str, text: str) -> dict:
    """Записать текстовый файл пака в DST (атомарно) И в M3-источник (ssh). Оба обязательны:
    только DST → orphan-cleanup синка сотрёт файл (в RAW его нет); только M3 → следующая джоба
    увидит его лишь после ночного синка. Возвращает отчёт по каждому назначению."""
    res = {"rel": rel, "dst": None, "m3": None}
    # DST
    try:
        _atomic_write_local(_dst_abs(rel), text)
        res["dst"] = {"ok": True, "path": _dst_abs(rel)}
        _touch_pack_cache_marker()
    except Exception as e:  # noqa: BLE001
        res["dst"] = {"ok": False, "error": str(e)[:200]}
    # M3-источник
    ok, err = _ssh_write_m3(_m3_abs(rel), text)
    res["m3"] = {"ok": ok, "path": _m3_abs(rel), "error": err or None}
    res["ok"] = bool(res["dst"] and res["dst"]["ok"] and ok)
    return res


# ── бэкап пак-файлов перед ВЕЕРНОЙ записью ───────────────────────────────────
# Структура/профиль бэкапятся (_write_struct/_write_profile), а пак-файлы — НЕ бэкапились совсем,
# хотя save_assets переписывает до 773 файлов ОДНИМ кликом и той же операцией затирает M3-источник
# → откатить было нечем. Пишем ОДИН json-снимок {rel: прежний_текст|null} на операцию.
_PACK_BAK_DIR = _HERE / "slepki_pack_backups"
_PACK_BAK_KEEP = 20                    # ротация: бэкапы синкаются Mutagen-ом, расти без предела нельзя


def _backup_pack_files(rels: list[str], tag: str) -> str | None:
    """Снимок текущего содержимого DST-копий `rels` в один json. → путь бэкапа (или None)."""
    if not rels:
        return None
    snap: dict[str, str | None] = {}
    for rel in rels:
        try:
            with open(_dst_abs(rel), encoding="utf-8") as f:
                snap[rel] = f.read()
        except OSError:
            snap[rel] = None           # файла не было — при откате его надо удалить, не создать
    try:
        _PACK_BAK_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dst = _PACK_BAK_DIR / f"{ts}_{tag}.json"
        _atomic_write_local(str(dst), json.dumps(
            {"ts": ts, "tag": tag, "files": snap}, ensure_ascii=False, indent=1))
        old = sorted(_PACK_BAK_DIR.glob("*.json"))
        for p in old[:-_PACK_BAK_KEEP]:            # ротация: держим только последние N
            try:
                p.unlink()
            except OSError:
                pass
        return str(dst)
    except Exception:  # noqa: BLE001 — бэкап best-effort, но его отсутствие обязано быть видно
        return None


# ── бэкап структурных файлов ─────────────────────────────────────────────────
def _backup(path: Path) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_name(f"{path.name}.editbak.{ts}")
    dst.write_bytes(path.read_bytes())
    return str(dst)


# ── аудит-лог ────────────────────────────────────────────────────────────────
def _audit(action: str, actor: str, spec: dict, result: dict) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "action": action,
           "actor": actor or "?", "spec": spec, "ok": bool(result.get("ok")),
           "result": {k: v for k, v in result.items() if k != "spec"}}
    # локальный jsonl (Mutagen-синк, всегда виден)
    try:
        with open(_AUDIT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    # Victory (best-effort — БД может лежать, не роняем правку)
    try:
        conn = _victory_conn_rw()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.direct_slepki_edits (
                    id         bigserial PRIMARY KEY,
                    ts         timestamptz NOT NULL DEFAULT now(),
                    action     text,
                    actor      text,
                    ok         boolean,
                    spec       jsonb,
                    result     jsonb
                )""")
            cur.execute(
                "INSERT INTO public.direct_slepki_edits (action, actor, ok, spec, result) "
                "VALUES (%s,%s,%s,%s,%s)",
                (action, actor or "?", bool(result.get("ok")),
                 json.dumps(spec, ensure_ascii=False),
                 json.dumps(rec["result"], ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


# ── чтение json структурных файлов ───────────────────────────────────────────
def _load_struct(*, mutable: bool = True) -> dict:
    """Структура разбита на per-slepok файлы (direct/slepki/) — собираем единый словарь.

    По умолчанию отдаём ПРИВАТНУЮ глубокую копию: все структурные правки (aon/aoff, add/remove
    ct, name_override) мутируют возвращённый объект ДО preflight, а при отказе просто делают
    `return` — без копии отклонённая правка оставалась в общем кэше `slepki_store` и уезжала на
    диск следующей УСПЕШНОЙ правкой (`_write_struct` пишет все part-файлы из своего снимка).
    Копия стоит ~92 мс (замер, 19.8 MiB) — на правку это ничто; для READ-ONLY обходов
    (`_iter_tp_ct`, экспорт xlsx) зовите `mutable=False`, там копия не нужна.
    """
    from . import slepki_store as _sstore
    return _sstore.assemble(mutable=mutable)


def _load_profile() -> dict:
    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


def _write_struct(struct: dict) -> str:
    # Пишем per-slepok файлы (атомарно, только изменившиеся) + _order.json. Бэкап — СТАРОЕ
    # содержимое изменившихся part-файлов (до записи), обратимость сохранена без 2.5 MiB-снимка.
    from . import slepki_store as _sstore
    dirs = struct.get("directologists") or []
    old: dict[str, bytes] = {}                 # снимок ДО записи по ключам входящих директологов
    for d in dirs:
        key = d.get("key")
        p = _sstore._part_path(key) if key else None
        if p and p.exists():
            old[key] = p.read_bytes()
    changed = _sstore.write_directologists(dirs)
    baks: list[str] = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for key in changed:
        if key in old:                         # был на диске → сохранить прежнюю версию
            dst = _sstore._part_path(key).with_name(f"{key}.json.editbak.{ts}")
            dst.write_bytes(old[key])
            baks.append(str(dst))
    return ";".join(baks)


def _write_profile(profile: dict) -> str:
    bak = _backup(_PROFILE_PATH)
    _atomic_write_local(str(_PROFILE_PATH),
                        json.dumps(profile, ensure_ascii=False, indent=1))
    _profile_invalidate()                 # сброс _TARGETING_PROFILE_CACHE (кэшируется!)
    return bak


# ── чтение ключей группы (для просмотра в UI) ────────────────────────────────
def _tp67_suppress_keywords_for_label(tp: str, position: str = "", target_label: str = "") -> bool:
    """tp6/tp7 pure auto/audience rows must not borrow common ct0000 keyword packs."""
    if tp not in ("tp6", "tp7"):
        return False
    label = str(target_label or position or "").strip().lower().replace("ё", "е")
    if not label:
        return False
    if re.search(r"(^|[^0-9a-zа-я])кс([^0-9a-zа-я]|$)|ключ", label):
        return False
    return "автотаргет" in label or "аудитори" in label


def read_group_keywords(site_type: str, tp: str, ct: str, slepok: str, group: str = "",
                        position: str = "", groups=None, target_label: str = "") -> dict:
    """{positive, minus, minus_shared, callouts, kw_source} по (тип сайта, tp, ct, слепок[, group]).
    minus_shared — библиотечный набор (просмотр, пер-ct); callouts — уточнения кампании.
    group непустой → per-group файлы ``{slepok}__{slug}...`` с фолбэком на легаси; ``group=""`` —
    прежнее поведение.

    ``kw_source``: ``"pack"`` — ключи из M3-пака; ``"real_library"`` — пак пуст и ключи взяты
    ТЕМ ЖЕ фолбэком, которым их берёт СОЗДАНИЕ кампании (см. ниже)."""
    # компоненты идут прямо в путь (kp._ct_dir / имя файла) → анти-traversal рубеж
    site_type = _safe_token(site_type, "site_type")
    tp = _safe_token(tp, "tp")
    slepok = _safe_token(slepok, "slepok")
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    kd = os.path.join(kp._ct_dir(site_type, tp, ctn), "keywords")
    group_list = []
    if groups is not None:
        group_list = [str(x).strip() for x in (groups or []) if str(x).strip()]
    elif group:
        group_list = [str(group).strip()]
    def _read_pack_for(read_tp: str) -> tuple[list[str], list[str], bool]:
        kd2 = os.path.join(kp._ct_dir(site_type, read_tp, ctn), "keywords")
        pos2: list[str] = []
        neg2: list[str] = []
        found2 = False
        if group_list:
            for one_group in group_list:
                slug2 = kp._group_slug(one_group)
                if not slug2:
                    continue
                p2, fp2 = kp._read_lines_opt(os.path.join(kd2, f"{slepok}__{slug2}.txt"))
                n2, fm2 = kp._read_lines_opt(os.path.join(kd2, f"{slepok}__{slug2}_minus.txt"))
                if fp2:
                    pos2.extend(p2)
                    found2 = True
                if fm2:
                    neg2.extend(n2)
            if not found2:
                pos2 = kp._read_lines(os.path.join(kd2, f"{slepok}.txt"))
            if not neg2:
                neg2 = kp._read_lines(os.path.join(kd2, f"{slepok}_minus.txt"))
        else:
            pos2 = kp._read_lines(os.path.join(kd2, f"{slepok}.txt"))
            neg2 = kp._read_lines(os.path.join(kd2, f"{slepok}_minus.txt"))
        return pos2, neg2, found2

    def _has_real_positive(lines: list[str]) -> bool:
        return any(str(x or "").strip() and str(x or "").strip().lower() != "---autotargeting"
                   for x in (lines or []))

    suppress_tp67_keywords = _tp67_suppress_keywords_for_label(tp, position, target_label)
    pos: list[str] = []
    neg: list[str] = []
    pos, neg, _ = _read_pack_for(tp)
    if suppress_tp67_keywords:
        pos = []
    neg_shared = kp._read_lines(os.path.join(kd, f"{slepok}_minus_shared.txt"))
    callouts = kp.read_callouts(site_type, tp, ctn, slepok, group=group)
    kw_source = "autotargeting" if suppress_tp67_keywords else "pack"
    if not suppress_tp67_keywords and tp in ("tp2", "tp4") and not _has_real_positive(pos):
        donors = ("tp2", "tp1") if tp == "tp4" else ("tp1",)
        for donor_tp in donors:
            f_pos, f_neg, _ = _read_pack_for(donor_tp)
            if _has_real_positive(f_pos):
                pos = list(f_pos)
                if not neg:
                    neg = list(f_neg or [])
                kw_source = f"{donor_tp}_fallback"
                break
    if not suppress_tp67_keywords and tp in ("tp6", "tp7") and position and len(group_list) > 1:
        try:
            f_pos, f_neg = _tp67_keywords_for_groups(
                slepok, site_type, tp, ctn, "", position or None, None, groups=group_list
            )
        except Exception:  # noqa: BLE001
            f_pos, f_neg = [], []
        if f_pos:
            pos, neg, kw_source = list(f_pos), list(f_neg or []), "merged"
    # tp6/tp7: M3-пака может не быть вовсе, а ключи всё равно уедут в кабинет — СОЗДАНИЕ берёт их
    # фолбэком из библиотеки реальных UAC-payload (create_set_context._tp67_keywords_for →
    # tp67_real_keywords.json). Без этого же фолбэка карточка врала: dmp/tp6/ct0834 показывал
    # «Автотаргетинг — ключевых слов в паке нет» при 69 реальных фразах. Зовём РОВНО ту функцию,
    # что и создание (пак → библиотека → цепочка tp7↦tp6), логику чтения не дублируем.
    # city="" — аккаунта в контексте структуры слепка нет; без своего города гео-фильтр ничего
    # не режет (city_morph._drop_foreign_city_keywords:191), т.е. показываем ДО-гео состав.
    #
    # Режим tp6/tp7 больше НЕ выводится regex-ом из display-имени: создание считает его по
    # содержимому структуры/пака в tp67_struct_expectations. Поэтому просмотр делает тот же
    # group-aware lookup и показывает fallback-ключи только если тот путь реально нашёл фразы.
    # position пуст (режим/позиция неизвестны) → фолбэк НЕ включаем: остаётся прежнее поведение
    # «только пак».
    if not suppress_tp67_keywords and not pos and tp in ("tp6", "tp7") and position:
        try:
            f_pos, f_neg = _tp67_keywords_for(
                slepok, site_type, tp, ctn, "", position or None, None, group=group
            )
        except Exception:  # noqa: BLE001 — фолбэк best-effort, карточка не должна падать
            f_pos, f_neg = [], []
        if f_pos:
            pos, kw_source = list(f_pos), "real_library"
            if not neg:
                neg = list(f_neg or [])
    pos = normalize_geo_lines(pos, dedup=True)
    return {"positive": kp._dedup(pos), "minus": kp._dedup(neg),
            "minus_shared": kp._dedup(neg_shared), "callouts": callouts,
            "kw_source": kw_source}


# ── ПРАВКА 1: ключи группы ───────────────────────────────────────────────────
def apply_edit_keywords(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, ct, positive?:[...], minus?:[...], minus_shared?:[...]}.
    Переписывает <slepok>.txt (positive) и <slepok>_minus.txt (per-slepok minus) в DST+M3.

    КАЖДЫЙ из трёх наборов пишется ТОЛЬКО если его ключ есть в spec (2026-07-19: раньше
    positive/minus писались всегда, и правка одного minus_shared зануляла бы соседние файлы).
    Карточка КАМПАНИИ шлёт ровно один ключ — ``minus_shared``.

    Библиотечные («общие») минусы <slepok>_minus_shared.txt редактируются ТОЛЬКО если ключ
    ``minus_shared`` присутствует в spec (иначе файл НЕ трогаем — обратная совместимость).
    ⚠️ SCOPE библиотечной правки — строго ЭТОТ (slepok, site_type, tp, ct):
      • имя файла всегда с префиксом ``{slepok}_`` → правка физически НЕ может задеть минусы
        ДРУГИХ слепков (у каждого слепка свой файл), даже если они «тянут ту же библиотеку»;
      • путь фиксирован UI-контекстом (site_type/tp/ct) → другие типы сайта / tp / ct не трогаются.
    Библиотека реально идентична по всем ct одного (slepok, site_type, tp) — здесь МЫ намеренно
    НЕ размножаем правку на соседние ct (это отдельная, более широкая операция): пишем ровно в
    ct, который редактируют. Так исключён клоббер возможных ct-вариаций и тяжёлый fan-out."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp = (spec.get("tp") or "").strip()
    ct = (spec.get("ct") or "").strip()
    group = (spec.get("group") or "").strip()   # per-adgroup (опц.): непустой → {slepok}__{slug}...
    if not (slepok and site_type and tp and ct):
        return {"ok": False, "error": "нужны slepok/site_type/tp/ct"}
    has_pos = "positive" in spec              # ключ отсутствует → файл НЕ трогаем (не зануляем)
    has_neg = "minus" in spec
    pos, e1 = _clean_kw_lines(spec.get("positive"), minus=False)
    neg, e2 = _clean_kw_lines(spec.get("minus"), minus=True)
    errs = e1 + e2
    has_shared = "minus_shared" in spec       # ключ отсутствует → библиотечный файл не трогаем
    neg_shared: list[str] = []
    if has_shared:
        neg_shared, e3 = _clean_kw_lines(spec.get("minus_shared"), minus=True)
        errs += e3
    if errs:
        return {"ok": False, "error": "валидация ключей: " + "; ".join(errs[:10])}
    if not (has_pos or has_neg or has_shared):
        return {"ok": False, "error": "нечего сохранять (нет positive/minus/minus_shared)"}
    with _EDIT_LOCK:
        r_pos = r_neg = None
        writes_ok = True
        if has_pos:
            rel_pos = _pack_rel(site_type, tp, ct, f"{slepok}.txt", group=group)
            r_pos = _dual_write_pack_file(rel_pos, "\n".join(pos) + ("\n" if pos else ""))
            writes_ok = writes_ok and bool(r_pos["ok"])
        if has_neg:
            rel_neg = _pack_rel(site_type, tp, ct, f"{slepok}_minus.txt", group=group)
            r_neg = _dual_write_pack_file(rel_neg, "\n".join(neg) + ("\n" if neg else ""))
            writes_ok = writes_ok and bool(r_neg["ok"])
        r_shared = None
        if has_shared:
            # shared-минус — ОБЩИЙ пер-ct (без слага): group здесь НЕ применяем.
            rel_shared = _pack_rel(site_type, tp, ct, f"{slepok}_minus_shared.txt")
            r_shared = _dual_write_pack_file(
                rel_shared, "\n".join(neg_shared) + ("\n" if neg_shared else ""))
            writes_ok = writes_ok and bool(r_shared["ok"])
        result = {"ok": writes_ok,
                  "positive_rows": (len(pos) if has_pos else None),
                  "minus_rows": (len(neg) if has_neg else None),
                  "minus_shared_rows": (len(neg_shared) if has_shared else None),
                  "write": {"positive": r_pos, "minus": r_neg, "minus_shared": r_shared}}
    _audit("edit_keywords", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp, "ct": ct,
            "positive_rows": (len(pos) if has_pos else None),
            "minus_rows": (len(neg) if has_neg else None),
            "minus_shared_rows": (len(neg_shared) if has_shared else None)}, result)
    return result


# ПРАВКА 1b `apply_edit_callouts` удалена 2026-07-19 как мёртвая (роут /api/slepki/edit_callouts
# снят, вызывающих нет). `_pack_rel_callouts` ОСТАЁТСЯ — им пользуется apply_save_assets.


# ── ассеты слепка (агрегат уточнений + библиотечных минусов по слепку × тип сайта) ──
def _iter_tp_ct(slepok: str, site_type: str) -> list[tuple[str, str]]:
    """Уникальные (tp_code, ct) пары структуры для (слепок, тип сайта).
    ct = ct#### из gc элемента (нормализованный); gc без ct → GENERAL_CT (ct0000).
    Порядок стабилен (первое вхождение). Не найден слепок/тип → []."""
    struct = _load_struct(mutable=False)      # read-only обход → копия не нужна
    d = _find_dir(struct, slepok)
    s = _find_site(d, site_type) if d else None
    if not s:
        return []
    seen: set = set()
    out: list[tuple[str, str]] = []
    for t in (s.get("tp") or []):
        tp = (t.get("code") or "").strip()
        if not tp:
            continue
        for g in _iter_containers(t):
            for it in (g.get("items") or []):
                if not isinstance(it, dict):
                    continue
                ct = kp._norm_ct(_gc_ct(it.get("gc") or "")) or kp.GENERAL_CT
                key = (tp, ct)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


_ASSET_KINDS = ("callouts", "minus_shared")


def _asset_rel(kind: str, site_type: str, tp: str, ct: str, slepok: str) -> str:
    """Пак-файл ассета для одного (tp,ct): callouts/{slepok}.txt | keywords/{slepok}_minus_shared.txt."""
    if kind == "callouts":
        return _pack_rel_callouts(site_type, tp, ct, f"{slepok}.txt")
    return _pack_rel(site_type, tp, ct, f"{slepok}_minus_shared.txt")


def _asset_pair_values(kind: str, slepok: str, site_type: str, tp: str, ct: str,
                       strict: bool = False) -> list[str]:
    """Текущий набор ассета в ОДНОМ (tp,ct). Нет файла → []. Читаем теми же примитивами,
    что и движок создания (kp), чтобы редактор не разошёлся с ним в трактовке файла.

    ⚠️ ``strict`` разводит ДВА смысла пустого результата, которые нельзя путать на ЗАПИСИ:
    «файла нет» (ct правда пуст) и «файл есть, но не прочитался» (права/гонка/битая кодировка).
    На чтении панели (``strict=False``) сбой глушится — панель не должна падать из-за одного
    битого файла. На записи (``strict=True``) сбой ОБЯЗАН всплыть: принять живой ct за пустой
    значит записать в него одну лишь дельту и молча стереть прежний набор.
    """
    try:
        if kind == "callouts":
            return list(kp.read_callouts(site_type, tp, ct, slepok))
        kd = os.path.join(kp._ct_dir(site_type, tp, ct), "keywords")
        return kp._dedup(kp._read_lines(os.path.join(kd, f"{slepok}_minus_shared.txt")))
    except Exception:  # noqa: BLE001 — отсутствующий/битый файл не должен ронять чтение всей панели
        if strict:
            raise
        return []


def read_assets(slepok: str, site_type: str) -> dict:
    """Агрегат УНИКАЛЬНЫХ уточнений (callouts) и библиотечных минус-слов (minus_shared) по ВСЕМ
    (tp,ct) слепка × тип сайта. Read-only (вызывается синхронно из web). Не падает без файлов.

    Наборы по ct РЕАЛЬНО различаются (замер 2026-07-19: scherbakova — 81 разный набор callouts на
    646 пар), поэтому кроме объединения отдаём и КАРТИНУ различий, иначе директолог их не видит:
      • ``coverage[kind][значение]`` — в скольких (tp,ct) значение присутствует (``< pairs`` = не везде);
      • ``variants[kind]`` — сколько РАЗНЫХ наборов по ct (1 = все ct одинаковы).

    ⚠️ Контракт: ``slepok``/``site_type`` проходят `_safe_token` → ПУСТАЯ строка даёт ``ValueError``,
    а не ``{}`` (до 2026-07-19 пустой ввод возвращал пустой результат). Единственный вызывающий —
    роут `/api/slepki/assets`, который отсекает пустые значения раньше (400)."""
    slepok = _safe_token(slepok, "slepok")       # компоненты идут в путь → анти-traversal
    site_type = _safe_token(site_type, "site_type")
    try:
        pairs = _iter_tp_ct(slepok, site_type)
    except Exception:  # noqa: BLE001
        pairs = []
    cov: dict = {k: {} for k in _ASSET_KINDS}
    variants: dict = {k: set() for k in _ASSET_KINDS}
    for tp, ct in pairs:
        for kind in _ASSET_KINDS:
            vals = _asset_pair_values(kind, slepok, site_type, tp, ct)
            for v in dict.fromkeys(vals):        # значение из одного ct считаем один раз
                cov[kind][v] = cov[kind].get(v, 0) + 1
            variants[kind].add(frozenset(x.lower() for x in vals))
    return {"callouts": list(cov["callouts"]),          # порядок = первое вхождение (как было)
            "minus_shared": list(cov["minus_shared"]),
            "pairs": len(pairs),
            "coverage": {k: cov[k] for k in _ASSET_KINDS},
            "variants": {k: len(variants[k]) for k in _ASSET_KINDS}}


def _asset_delta(kind: str, spec: dict, slepok: str, site_type: str,
                 pairs: list) -> tuple[list[str], list[str], list[str]] | None:
    """Дельта правки ассета → (add, remove, ошибки). None = этот вид ассета не правился.

    Основной путь — КЛИЕНТСКАЯ дельта ``{kind}_add`` / ``{kind}_remove``: она устойчива к гонке
    (значение, добавленное кем-то другим после загрузки панели, не удаляется).
    Легаси-путь — прислан ФИНАЛЬНЫЙ список ``{kind}`` (старый клиент / джоба, уже стоящая в
    очереди): дельту выводим относительно ТЕКУЩЕГО объединения, а не пишем список веером."""
    is_minus = (kind == "minus_shared")
    if f"{kind}_add" in spec or f"{kind}_remove" in spec:
        add, e1 = _clean_kw_lines(spec.get(f"{kind}_add"), minus=is_minus)
        rem, e2 = _clean_kw_lines(spec.get(f"{kind}_remove"), minus=is_minus)
        return add, rem, e1 + e2
    if kind in spec:
        final, e1 = _clean_kw_lines(spec.get(kind), minus=is_minus)
        union: list[str] = []
        for tp, ct in pairs:
            union.extend(_asset_pair_values(kind, slepok, site_type, tp, ct))
        union = kp._dedup(union)
        fin_l = {x.lower() for x in final}
        uni_l = {x.lower() for x in union}
        return ([x for x in final if x.lower() not in uni_l],
                [x for x in union if x.lower() not in fin_l], e1)
    return None


def apply_save_assets(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, callouts_add?/callouts_remove?, minus_shared_add?/…_remove?}.

    МОДЕЛЬ ЗАПИСИ (2026-07-19, решение Семёна «редактор обязан НЕ терять per-ct специфику»):
      • читаем — ОБЪЕДИНЕНИЕ по всем (tp,ct) + карта различий (`read_assets`);
      • пишем — только ДЕЛЬТУ («добавил X», «удалил Y»), применяя её к КАЖДОМУ (tp,ct)
        поверх его СОБСТВЕННОГО набора: ``новый = текущий − remove + add``;
      • (tp,ct), у которого от этого ничего не меняется, НЕ переписывается вовсе — ни DST, ни M3.
    До этой правки веером писался финальный union во ВСЕ ct → 81 разный набор callouts у
    scherbakova схлопывался в один необратимо (замер 2026-07-19).

    Файлы: callouts → callouts/{slepok}.txt, minus_shared → keywords/{slepok}_minus_shared.txt.
    Вид ассета, которого нет в spec, не трогаем совсем (не затираем библиотеку пустым)."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    if not (slepok and site_type):
        return {"ok": False, "error": "нужны slepok/site_type"}
    with _EDIT_LOCK:
        pairs = _iter_tp_ct(slepok, site_type)
        if not pairs:
            return {"ok": False, "error": f"нет (tp,ct) в структуре для {slepok}/{site_type}"}
        deltas: dict = {}
        errs: list[str] = []
        for kind in _ASSET_KINDS:
            d = _asset_delta(kind, spec, slepok, site_type, pairs)
            if d is None:
                continue
            deltas[kind] = (d[0], d[1])
            errs += d[2]
        if not deltas:
            return {"ok": False, "error": "нечего сохранять (нет callouts/minus_shared)"}
        if errs:
            return {"ok": False, "error": "валидация: " + "; ".join(errs[:10])}
        write: dict = {}
        backups: dict = {}
        changed_cnt: dict = {}
        delta_rep: dict = {}
        ok = True
        for kind, (add, rem) in deltas.items():
            delta_rep[kind] = {"add": len(add), "remove": len(rem)}
            rem_l = {x.lower() for x in rem}
            changed: dict = {}                 # rel → новый текст (у каждого ct свой)
            for tp, ct in pairs:
                # strict: нечитаемый файл здесь НЕ считается пустым ct (иначе дельта затрёт набор).
                try:
                    cur = _asset_pair_values(kind, slepok, site_type, tp, ct, strict=True)
                except Exception as e:  # noqa: BLE001 — отменяем правку целиком, не гадаем
                    return {"ok": False,
                            "error": f"не прочитан текущий набор {kind} в {tp}/{ct} "
                                     f"({type(e).__name__}: {e}) — запись отменена"}
                new = [x for x in cur if x.lower() not in rem_l]
                have = {x.lower() for x in new}
                for a in add:
                    if a.lower() not in have:
                        new.append(a)
                        have.add(a.lower())
                if new == cur:                 # этот ct правка не касается → файл не трогаем
                    continue
                changed[_asset_rel(kind, site_type, tp, ct, slepok)] = (
                    "\n".join(new) + ("\n" if new else ""))
            changed_cnt[kind] = len(changed)
            if not changed:
                write[kind] = {"ok": True, "count": 0, "skipped": len(pairs)}
                continue
            # Запись необратима (тем же кликом перезаписывается и M3-источник) → снимок ДО.
            # Бэкап не удался → НЕ пишем: молча потерять прежний набор дороже, чем отменить правку.
            bak = _backup_pack_files(list(changed), f"{kind}_{slepok}_{site_type}")
            backups[kind] = bak
            if not bak:
                return {"ok": False,
                        "error": f"бэкап пак-файлов ({kind}) не создан — запись отменена"}
            r = _dual_write_pack_files_map(changed)
            write[kind] = r
            ok = ok and bool(r["ok"])
        result = {"ok": ok, "pairs": len(pairs), "delta": delta_rep,
                  "changed_files": changed_cnt, "backups": backups, "write": write}
    _audit("save_assets", actor,
           {"slepok": slepok, "site_type": site_type, "pairs": len(pairs),
            "delta": delta_rep, "changed_files": changed_cnt}, result)
    return result


# ── именованные наборы минус-слов (несколько на слепок × тип сайта) ───────────
def _norm_set_name(raw) -> str:
    """Имя набора: trim + вырезаем управляющие символы; каскад пробелов → один."""
    s = (str(raw) if raw is not None else "").strip()
    s = _CTRL_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def read_minus_sets(slepok: str, site_type: str) -> dict:
    """Прочитать ИМЕНОВАННЫЕ наборы минус-слов слепка × тип сайта из ЛОКАЛЬНОЙ копии пака.
    Возвращает {"sets": [{"name","phrases":[...]}], "count": N}. Read-only (синхронно из web).
    Файла нет / битый JSON → пустой список (не падаем)."""
    slepok = (slepok or "").strip()
    site_type = (site_type or "").strip()
    out: list[dict] = []
    if slepok and site_type:
        path = _dst_abs(_pack_rel_minus_sets(site_type, slepok))
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        # формат файла: {"slug","site_type","sets":[...]} ИЛИ голый список [...] — принимаем оба
        raw_sets = None
        if isinstance(data, dict):
            raw_sets = data.get("sets")
        elif isinstance(data, list):
            raw_sets = data
        for it in (raw_sets or []):
            if not isinstance(it, dict):
                continue
            name = _norm_set_name(it.get("name"))
            phrases = [str(p) for p in (it.get("phrases") or []) if str(p).strip()]
            if name or phrases:
                out.append({"name": name, "phrases": phrases})
    return {"sets": out, "count": len(out)}


def apply_save_minus_sets(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, sets:[{name, phrases:[...]}]}.
    ЗАМЕНЯЕТ (перезаписывает целиком) файл именованных наборов слепка × тип сайта — dual-write
    DST+M3. Пустой список sets → пишем пустой файл (легитимное «наборов нет»). Каждый набор:
    имя (обязательно) + фразы (валидация как минус-слова, дедуп caseless внутри набора).
    НЕ трогает {slepok}_minus_shared.txt (библиотека движка) и keywords/callouts."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    if not (slepok and site_type):
        return {"ok": False, "error": "нужны slepok/site_type"}
    raw_sets = spec.get("sets")
    if not isinstance(raw_sets, list):
        return {"ok": False, "error": "нужен список sets"}
    clean: list[dict] = []
    errs: list[str] = []
    seen_names: set = set()
    for idx, it in enumerate(raw_sets):
        if not isinstance(it, dict):
            errs.append(f"набор #{idx + 1}: не объект")
            continue
        name = _norm_set_name(it.get("name"))
        phrases, e = _clean_kw_lines(it.get("phrases"), minus=True)
        errs += [f"«{name or ('#' + str(idx + 1))}»: {x}" for x in e]
        if not name and not phrases:
            continue  # полностью пустой набор — тихо отбрасываем
        if not name:
            errs.append(f"набор #{idx + 1}: пустое имя")
            continue
        key = name.lower()
        if key in seen_names:
            errs.append(f"дубль имени набора: «{name}»")
            continue
        seen_names.add(key)
        clean.append({"name": name, "phrases": phrases})
    if errs:
        return {"ok": False, "error": "валидация: " + "; ".join(errs[:10])}
    payload = {"slug": slepok, "site_type": site_type, "sets": clean}
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    with _EDIT_LOCK:
        rel = _pack_rel_minus_sets(site_type, slepok)
        r = _dual_write_pack_file(rel, text)
    result = {"ok": bool(r["ok"]), "sets": len(clean),
              "phrases_total": sum(len(s["phrases"]) for s in clean), "write": r}
    _audit("save_minus_sets", actor,
           {"slepok": slepok, "site_type": site_type, "sets": len(clean),
            "phrases_total": result["phrases_total"]}, result)
    return result


# ── помощники структуры ──────────────────────────────────────────────────────
def _find_dir(struct: dict, slepok: str) -> dict | None:
    return next((d for d in struct.get("directologists", [])
                 if (d.get("key") or "") == slepok), None)


def _find_site(d: dict, site_type: str) -> dict | None:
    return next((s for s in d.get("site_types", [])
                 if (s.get("name") or "") == site_type), None)


def _find_tp(s: dict, tp: str) -> dict | None:
    return next((t for t in s.get("tp", []) if (t.get("code") or "") == tp), None)


def _iter_containers(t: dict):
    """Ярд контейнеров tp: (container_dict, list_owner). groups и splits.groups."""
    for g in (t.get("groups") or []):
        yield g
    for sp in (t.get("splits") or []):
        for g in (sp.get("groups") or []):
            yield g


def _gc_ct(gc: str) -> str:
    m = re.search(r"ct\d{4}", gc or "")
    return m.group(0) if m else ""


def _set_gc_autotarget(gc: str, mode: str) -> str:
    """Переписать ag_part2-токен (aon/aoff) в gc; иначе вернуть как есть."""
    m = _GC_RE.match(gc or "")
    if not m:
        return gc
    parts = list(m.groups())
    parts[1] = mode
    return "_".join(parts)


# ── ПРАВКА 2: тумблер aon↔aoff сегмента ──────────────────────────────────────
def apply_toggle_aon_aoff(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, segment, mode: 'aon'|'aoff'}.
    aon=Автотаргет, aoff=КС. Правит targeting_profile.json (какой режим ведёт сегмент) и
    синхронизирует ag_part2-токен в gc элементов slepki_structure.json того же сегмента."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp = (spec.get("tp") or "").strip()
    segment = (spec.get("segment") or "").strip()
    mode = (spec.get("mode") or "").strip()
    if mode not in ("aon", "aoff"):
        return {"ok": False, "error": "mode должен быть aon|aoff"}
    if not (slepok and site_type and tp and segment):
        return {"ok": False, "error": "нужны slepok/site_type/tp/segment"}
    target_key = "Автотаргет" if mode == "aon" else "КС"
    with _EDIT_LOCK:
        profile = _load_profile()
        # канон сегмента профиля: 'Общее' классификатора ↔ 'общая' профиля
        seg_tps = ((profile.get(slepok, {}) or {}).get(site_type, {}) or {}).get(tp, {}) or {}
        pkey = next((k for k in seg_tps if k.strip().lower().startswith(segment.strip().lower()[:3])
                     or (segment.lower().startswith("общ") and k.lower().startswith("общ"))), None)
        if pkey is None:
            return {"ok": False, "error": f"сегмент '{segment}' не найден в профиле {slepok}/{site_type}/{tp}"}
        modes = seg_tps.get(pkey, {}) or {}
        cnt = sum(int(v or 0) for v in modes.values()) or 1
        new_profile = json.loads(json.dumps(profile))     # deep copy
        new_profile[slepok][site_type][tp][pkey] = {target_key: cnt}

        # структура: синхронизировать ag_part2-токен gc элементов ЭТОГО сегмента
        struct = _load_struct()
        d = _find_dir(struct, slepok)
        s = _find_site(d, site_type) if d else None
        t = _find_tp(s, tp) if s else None
        touched = 0
        if t is not None:
            for g in _iter_containers(t):
                for it in (g.get("items") or []):
                    if not isinstance(it, dict):
                        continue
                    gc = it.get("gc") or ""
                    ct = _gc_ct(gc)
                    if not ct or _ct_segment(ct) != segment:
                        continue
                    new_gc = _set_gc_autotarget(gc, mode)
                    if new_gc != gc:
                        it["gc"] = new_gc
                        touched += 1

        # preflight на ПРЕДЛАГАЕМОМ состоянии
        viol = _preflight_mod().preflight_dict(struct, new_profile)
        if viol:
            return {"ok": False, "error": "preflight отказ", "violations": viol[:20]}

        bak_p = _write_profile(new_profile)
        bak_s = _write_struct(struct) if touched else None
        result = {"ok": True, "segment": segment, "mode": mode, "profile_count": cnt,
                  "gc_touched": touched, "backup_profile": bak_p, "backup_struct": bak_s}
    _audit("toggle_aon_aoff", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp,
            "segment": segment, "mode": mode}, result)
    return result


# ── ПРАВКА 3/4: add/remove ct-группа ─────────────────────────────────────────
def _gen_item_desc(tp: str, brand: str, segment: str) -> str:
    """Описание элемента (поле t), как в реальной структуре: «РСЯ {Бренд} марка» / «Поиск …»."""
    surf = {"tp1": "РСЯ", "tp2": "Поиск", "tp3": "ТГ РСЯ", "tp4": "Поиск",
            "tp5": "Поиск", "tp6": "МК", "tp7": "ТК",
            "tp8": "Telegram", "tp9": "Max", "tp10": "ТГ+Макс"}.get(tp, tp)
    seg_word = {"Марки": "марка", "Модели": "модель", "Общее": "общая"}.get(segment, "")
    return f"{surf} {brand} {seg_word}".strip()


def apply_add_ct_group(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, ct(4-знач), mode: aon|aoff, region, fmt(ct3), age, gender, desc?}.
    Собирает gc ИЗ КОМПОНЕНТОВ (сырой gc не принимаем), добавляет элемент {c,t,gc} в подходящий
    контейнер (по сегменту ct). ct обязан быть в зарегистрированном кодере (ag_part1_map)."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp = (spec.get("tp") or "").strip()
    ct = (spec.get("ct") or "").strip()
    mode = (spec.get("mode") or "aon").strip()
    region = (spec.get("region") or "r0000").strip()
    fmt = (spec.get("fmt") or "ct001").strip()
    age = (spec.get("age") or "ag011").strip()
    gender = (spec.get("gender") or "g00").strip()
    interest = (spec.get("interest") or "n000").strip()
    if not (slepok and site_type and tp and ct):
        return {"ok": False, "error": "нужны slepok/site_type/tp/ct"}
    if not _CT4_RE.match(ct):
        return {"ok": False, "error": "ct должен быть ct#### (4 цифры)"}
    if ct not in _ag_part1_map():
        return {"ok": False, "error": f"ct {ct} НЕ зарегистрирован в кодере (ag_part1)"}
    if mode not in ("aon", "aoff"):
        return {"ok": False, "error": "mode aon|aoff"}
    # собрать gc из компонентов и провалидировать формат
    gc = f"{ct}_{mode}_{interest}_{region}_{fmt}_{age}_{gender}"
    if not _GC_RE.match(gc):
        return {"ok": False, "error": f"собранный gc невалиден: {gc}"}
    brand = _ag_part1_map().get(ct, ct)
    segment = _ct_segment(ct)
    desc = (spec.get("desc") or "").strip() or _gen_item_desc(tp, brand, segment)
    camp = (spec.get("c") or f"{tp}_cpc_site").strip()
    with _EDIT_LOCK:
        struct = _load_struct()
        d = _find_dir(struct, slepok)
        s = _find_site(d, site_type) if d else None
        t = _find_tp(s, tp) if s else None
        if t is None:
            return {"ok": False, "error": f"нет узла {slepok}/{site_type}/{tp} в структуре"}
        # дубль?
        for g in _iter_containers(t):
            for it in (g.get("items") or []):
                if isinstance(it, dict) and it.get("gc") == gc:
                    return {"ok": False, "error": f"группа с gc {gc} уже есть"}
        # выбрать контейнер: тот, где преобладает тот же сегмент; иначе первый; иначе создать
        target = None
        best = -1
        for g in _iter_containers(t):
            same = sum(1 for it in (g.get("items") or [])
                       if isinstance(it, dict) and _ct_segment(_gc_ct(it.get("gc") or "")) == segment)
            if same > best:
                best, target = same, g
        if target is None:
            target = {"name": f"{tp} · {segment}", "items": []}
            t.setdefault("groups", []).append(target)
        target.setdefault("items", []).append({"c": camp, "t": desc, "gc": gc})

        viol = _preflight_mod().preflight_dict(struct, _load_profile())
        if viol:
            return {"ok": False, "error": "preflight отказ", "violations": viol[:20]}
        bak = _write_struct(struct)
        result = {"ok": True, "gc": gc, "ct": ct, "brand": brand, "segment": segment,
                  "container": target.get("name") or target.get("label"), "backup_struct": bak}
    _audit("add_ct_group", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp, "gc": gc}, result)
    return result


def apply_remove_ct_group(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, gc}. Удаляет элемент(ы) с этим gc; чистит опустевший контейнер."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp = (spec.get("tp") or "").strip()
    gc = (spec.get("gc") or "").strip()
    if not (slepok and site_type and tp and gc):
        return {"ok": False, "error": "нужны slepok/site_type/tp/gc"}
    with _EDIT_LOCK:
        struct = _load_struct()
        d = _find_dir(struct, slepok)
        s = _find_site(d, site_type) if d else None
        t = _find_tp(s, tp) if s else None
        if t is None:
            return {"ok": False, "error": f"нет узла {slepok}/{site_type}/{tp}"}
        removed = 0
        # удалить из groups
        for g in (t.get("groups") or []):
            before = len(g.get("items") or [])
            g["items"] = [it for it in (g.get("items") or [])
                          if not (isinstance(it, dict) and it.get("gc") == gc)]
            removed += before - len(g["items"])
        t["groups"] = [g for g in (t.get("groups") or []) if (g.get("items") or [])]
        # удалить из splits.groups
        for sp in (t.get("splits") or []):
            for g in (sp.get("groups") or []):
                before = len(g.get("items") or [])
                g["items"] = [it for it in (g.get("items") or [])
                              if not (isinstance(it, dict) and it.get("gc") == gc)]
                removed += before - len(g["items"])
            sp["groups"] = [g for g in (sp.get("groups") or []) if (g.get("items") or [])]
        t["splits"] = [sp for sp in (t.get("splits") or []) if (sp.get("groups") or [])]
        if removed == 0:
            return {"ok": False, "error": f"элемент с gc {gc} не найден"}

        viol = _preflight_mod().preflight_dict(struct, _load_profile())
        if viol:
            return {"ok": False, "error": "preflight отказ (удаление оставило пустой tp/группу)",
                    "violations": viol[:20]}
        bak = _write_struct(struct)
        result = {"ok": True, "gc": gc, "removed": removed, "backup_struct": bak}
    _audit("remove_ct_group", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp, "gc": gc}, result)
    return result


def apply_set_name_override(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, segment, mode, name_override}.
    Устанавливает / удаляет tp.name_overrides['{seg}|{mode}'] в slepki_structure.json.
    mode = 'КС' | 'Автотаргет' (пустой — ключ без mode, только по сегменту).
    name_override пустой → удалить переопределение."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp_code = (spec.get("tp") or "").strip()
    seg = (spec.get("segment") or "").strip()
    mode = (spec.get("mode") or "").strip()
    override = (spec.get("name_override") or "").strip()
    if not (slepok and site_type and tp_code and seg):
        return {"ok": False, "error": "нужны slepok/site_type/tp/segment"}
    key = f"{seg}|{mode}" if mode else seg
    with _EDIT_LOCK:
        struct = _load_struct()
        d = _find_dir(struct, slepok)
        s = _find_site(d, site_type) if d else None
        t = _find_tp(s, tp_code) if s else None
        if t is None:
            return {"ok": False, "error": f"нет {slepok}/{site_type}/{tp_code}"}
        overrides = t.setdefault("name_overrides", {})
        if override:
            overrides[key] = override
        elif key in overrides:
            del overrides[key]
        # не оставлять пустой dict
        if not overrides:
            t.pop("name_overrides", None)
        bak = _write_struct(struct)
        result = {"ok": True, "key": key, "override": override or None, "backup": bak}
    _audit("set_name_override", actor, spec, result)
    return result


# ── диспетчер из воркера ─────────────────────────────────────────────────────
def handle_job(body: dict) -> dict:
    """Вызывается воркером для _kind ∈ _EDIT_KINDS. body: {_kind, spec, _actor}."""
    kind = (body or {}).get("_kind") or ""
    spec = (body or {}).get("spec") or {}
    actor = (body or {}).get("_actor") or (body or {}).get("actor") or ""
    fn = {
        "edit_keywords": apply_edit_keywords,
        "save_assets": apply_save_assets,
        "save_minus_sets": apply_save_minus_sets,
        "toggle_aon_aoff": apply_toggle_aon_aoff,
        "add_ct_group": apply_add_ct_group,
        "remove_ct_group": apply_remove_ct_group,
        "set_name_override": apply_set_name_override,
    }.get(kind)
    if fn is None:
        return {"error": f"неизвестный edit-kind: {kind}"}
    try:
        r = fn(spec, actor)
    except Exception as e:  # noqa: BLE001
        import traceback
        return {"error": f"{kind}: {str(e)[:200]}", "trace": traceback.format_exc()[-800:]}
    if not r.get("ok"):
        return {"error": r.get("error") or f"{kind} не применён", "detail": r}
    return {"ok": True, "kind": kind, "detail": r}

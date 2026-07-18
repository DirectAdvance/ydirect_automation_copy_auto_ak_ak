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

# preflight — из scripts/ (тот же корень пакета)
import importlib.util as _ilu

_HERE = Path(__file__).resolve().parent
# slepki_structure.json больше не монолит — структура в direct/slepki/ (см. slepki_store),
# читается _load_struct()→assemble(), пишется _write_struct()→write_directologists().
_PROFILE_PATH = _HERE / "targeting_profile.json"
_AUDIT_JSONL = _HERE / "slepki_edits_audit.jsonl"

# Сериализует правки между собой в пределах процесса (доп. к бакет-гейту "" в воркере).
_EDIT_LOCK = threading.RLock()

_EDIT_KINDS = {"edit_keywords", "edit_callouts", "save_assets", "save_minus_sets",
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
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    return posixpath.join(site_type, tp, ctn, "keywords", _group_fname(fname, kp._group_slug(group)))


def _pack_rel_callouts(site_type: str, tp: str, ct: str, fname: str, group: str = "") -> str:
    """Относительный путь файла уточнений (callouts) внутри kontent_oktyabr.
    Зеркалит kontent_pack.read_callouts: {site_type}/{tp}/{ct}/callouts/{slepok}[__{slug}].txt."""
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    return posixpath.join(site_type, tp, ctn, "callouts", _group_fname(fname, kp._group_slug(group)))


def _pack_rel_minus_sets(site_type: str, slepok: str) -> str:
    """Относительный путь файла ИМЕНОВАННЫХ наборов минус-слов внутри kontent_oktyabr.
    Уровень (слепок × тип сайта): {site_type}/_minus_sets/{slepok}.json.
    НЕ per-(tp,ct) (в отличие от keywords/callouts): наборы — свойство кабинета целиком.
    Отдельно от {slepok}_minus_shared.txt (тот читает движок создания — не трогаем)."""
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


def _ssh_write_m3_many(remote_abs_list: list[str], text: str) -> tuple[bool, str]:
    """Записать ОДИН И ТОТ ЖЕ text во МНОЖЕСТВО файлов M3 за ОДНУ ssh-сессию (батч).
    Пишем text во временный файл, затем cp→временный per-файл + атомарный mv в цель
    (читатель create-РК видит либо старое, либо новое целое). Пустой список → no-op."""
    if not remote_abs_list:
        return True, ""
    tmp = f"/tmp/.slepki_asset.{int(time.time()*1000)}.{os.getpid()}"
    parts = [f"cat > {shlex.quote(tmp)}"]
    for ra in remote_abs_list:
        rdir = posixpath.dirname(ra)
        dtmp = f"{ra}.tmpasset"
        parts.append(
            f"mkdir -p {shlex.quote(rdir)} && cp {shlex.quote(tmp)} {shlex.quote(dtmp)} "
            f"&& mv -f {shlex.quote(dtmp)} {shlex.quote(ra)}")
    parts.append(f"rm -f {shlex.quote(tmp)}")
    cmd = " && ".join(parts)
    try:
        r = subprocess.run(
            kp._M3_SSH + [cmd],
            input=text, text=True, capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            return False, (r.stderr or "")[-300:]
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _dual_write_pack_files_same(rels: list[str], text: str) -> dict:
    """Записать один и тот же text во МНОЖЕСТВО пак-файлов: DST (локально, атомарно, по одному —
    быстро) + M3 (одна батч-ssh-сессия). Возвращает сводный отчёт. Оба назначения обязательны:
    только DST → orphan-cleanup синка сотрёт; только M3 → следующая джоба увидит после ночного синка."""
    dst_fail: list[dict] = []
    for rel in rels:
        try:
            _atomic_write_local(_dst_abs(rel), text)
        except Exception as e:  # noqa: BLE001
            dst_fail.append({"rel": rel, "error": str(e)[:120]})
    m3_ok, m3_err = _ssh_write_m3_many([_m3_abs(r) for r in rels], text)
    return {"ok": (not dst_fail) and m3_ok,
            "count": len(rels), "dst_ok": len(rels) - len(dst_fail),
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
    except Exception as e:  # noqa: BLE001
        res["dst"] = {"ok": False, "error": str(e)[:200]}
    # M3-источник
    ok, err = _ssh_write_m3(_m3_abs(rel), text)
    res["m3"] = {"ok": ok, "path": _m3_abs(rel), "error": err or None}
    res["ok"] = bool(res["dst"] and res["dst"]["ok"] and ok)
    return res


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
def _load_struct() -> dict:
    # Структура разбита на per-slepok файлы (direct/slepki/) — собираем единый словарь.
    from . import slepki_store as _sstore
    return _sstore.assemble()


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
def read_group_keywords(site_type: str, tp: str, ct: str, slepok: str, group: str = "",
                        position: str = "") -> dict:
    """{positive, minus, minus_shared, callouts, kw_source} по (тип сайта, tp, ct, слепок[, group]).
    minus_shared — библиотечный набор (просмотр, пер-ct); callouts — уточнения кампании.
    group непустой → per-group файлы ``{slepok}__{slug}...`` с фолбэком на легаси; ``group=""`` —
    прежнее поведение.

    ``kw_source``: ``"pack"`` — ключи из M3-пака; ``"real_library"`` — пак пуст и ключи взяты
    ТЕМ ЖЕ фолбэком, которым их берёт СОЗДАНИЕ кампании (см. ниже)."""
    ctn = kp._norm_ct(ct) or kp.GENERAL_CT
    kd = os.path.join(kp._ct_dir(site_type, tp, ctn), "keywords")
    slug = kp._group_slug(group)
    if slug:
        pos, fp = kp._read_lines_opt(os.path.join(kd, f"{slepok}__{slug}.txt"))
        if not fp:
            pos = kp._read_lines(os.path.join(kd, f"{slepok}.txt"))
        neg, fm = kp._read_lines_opt(os.path.join(kd, f"{slepok}__{slug}_minus.txt"))
        if not fm:
            neg = kp._read_lines(os.path.join(kd, f"{slepok}_minus.txt"))
    else:
        pos = kp._read_lines(os.path.join(kd, f"{slepok}.txt"))
        neg = kp._read_lines(os.path.join(kd, f"{slepok}_minus.txt"))
    neg_shared = kp._read_lines(os.path.join(kd, f"{slepok}_minus_shared.txt"))
    callouts = kp.read_callouts(site_type, tp, ctn, slepok, group=group)
    kw_source = "pack"
    # tp6/tp7: M3-пака может не быть вовсе, а ключи всё равно уедут в кабинет — СОЗДАНИЕ берёт их
    # фолбэком из библиотеки реальных UAC-payload (create_set_context._tp67_keywords_for →
    # tp67_real_keywords.json). Без этого же фолбэка карточка врала: dmp/tp6/ct0834 показывал
    # «Автотаргетинг — ключевых слов в паке нет» при 69 реальных фразах. Зовём РОВНО ту функцию,
    # что и создание (пак → библиотека → цепочка tp7↦tp6), логику чтения не дублируем.
    # city="" — аккаунта в контексте структуры слепка нет; без своего города гео-фильтр ничего
    # не режет (city_morph._drop_foreign_city_keywords:191), т.е. показываем ДО-гео состав.
    #
    # ГЕЙТ РЕЖИМА (обязателен): создание берёт ключи ТОЛЬКО для keyword-позиций
    # (create_set_master_product.py:124 `if _want_keywords`). Без гейта карточка автотаргет-строки
    # («МК - Общая - Автотаргетинг», tp6/tp7 авто-слепков) показала бы библиотечные ключи, которые
    # в кабинет НЕ уедут — то же враньё, только наизнанку. position пуст (режим неизвестен) →
    # фолбэк НЕ включаем: остаётся прежнее поведение «только пак».
    if (not pos and tp in ("tp6", "tp7") and position
            and _tp67_targeting_mode({"name": position}) == "keywords"):
        try:
            f_pos, f_neg = _tp67_keywords_for(slepok, site_type, tp, ctn, "", position or None, None)
        except Exception:  # noqa: BLE001 — фолбэк best-effort, карточка не должна падать
            f_pos, f_neg = [], []
        if f_pos:
            pos, kw_source = list(f_pos), "real_library"
            if not neg:
                neg = list(f_neg or [])
    return {"positive": kp._dedup(pos), "minus": kp._dedup(neg),
            "minus_shared": kp._dedup(neg_shared), "callouts": callouts,
            "kw_source": kw_source}


# ── ПРАВКА 1: ключи группы ───────────────────────────────────────────────────
def apply_edit_keywords(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, ct, positive:[...], minus:[...], minus_shared?:[...]}.
    Переписывает <slepok>.txt (positive) и <slepok>_minus.txt (per-slepok minus) в DST+M3.

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
    with _EDIT_LOCK:
        rel_pos = _pack_rel(site_type, tp, ct, f"{slepok}.txt", group=group)
        rel_neg = _pack_rel(site_type, tp, ct, f"{slepok}_minus.txt", group=group)
        r_pos = _dual_write_pack_file(rel_pos, "\n".join(pos) + ("\n" if pos else ""))
        r_neg = _dual_write_pack_file(rel_neg, "\n".join(neg) + ("\n" if neg else ""))
        writes_ok = bool(r_pos["ok"] and r_neg["ok"])
        r_shared = None
        if has_shared:
            # shared-минус — ОБЩИЙ пер-ct (без слага): group здесь НЕ применяем.
            rel_shared = _pack_rel(site_type, tp, ct, f"{slepok}_minus_shared.txt")
            r_shared = _dual_write_pack_file(
                rel_shared, "\n".join(neg_shared) + ("\n" if neg_shared else ""))
            writes_ok = writes_ok and bool(r_shared["ok"])
        result = {"ok": writes_ok,
                  "positive_rows": len(pos), "minus_rows": len(neg),
                  "minus_shared_rows": (len(neg_shared) if has_shared else None),
                  "write": {"positive": r_pos, "minus": r_neg, "minus_shared": r_shared}}
    _audit("edit_keywords", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp, "ct": ct,
            "positive_rows": len(pos), "minus_rows": len(neg),
            "minus_shared_rows": (len(neg_shared) if has_shared else None)}, result)
    return result


# ── ПРАВКА 1b: уточнения (callouts) кампании ──────────────────────────────────
def apply_edit_callouts(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, tp, ct, callouts:[...]}.
    Переписывает callouts/<slepok>.txt в DST+M3 (dual-write durable, переживает ночной синк
    sync_content_m3.py — callouts/ НЕ в его excludes). Читается kontent_pack.read_callouts.
    Уточнения — уровень КАМПАНИИ; ct = репрезентативный ct кампании (передаёт UI)."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    tp = (spec.get("tp") or "").strip()
    ct = (spec.get("ct") or "").strip()
    if not (slepok and site_type and tp and ct):
        return {"ok": False, "error": "нужны slepok/site_type/tp/ct"}
    # callouts — короткие фразы уточнений (не минус): trim + дедуп + контроль-символы + длина.
    calls, errs = _clean_kw_lines(spec.get("callouts"), minus=False)
    if errs:
        return {"ok": False, "error": "валидация уточнений: " + "; ".join(errs[:10])}
    with _EDIT_LOCK:
        rel = _pack_rel_callouts(site_type, tp, ct, f"{slepok}.txt")
        r = _dual_write_pack_file(rel, "\n".join(calls) + ("\n" if calls else ""))
        result = {"ok": bool(r["ok"]), "callouts_rows": len(calls), "write": {"callouts": r}}
    _audit("edit_callouts", actor,
           {"slepok": slepok, "site_type": site_type, "tp": tp, "ct": ct,
            "callouts_rows": len(calls)}, result)
    return result


# ── ассеты слепка (агрегат уточнений + библиотечных минусов по слепку × тип сайта) ──
def _iter_tp_ct(slepok: str, site_type: str) -> list[tuple[str, str]]:
    """Уникальные (tp_code, ct) пары структуры для (слепок, тип сайта).
    ct = ct#### из gc элемента (нормализованный); gc без ct → GENERAL_CT (ct0000).
    Порядок стабилен (первое вхождение). Не найден слепок/тип → []."""
    struct = _load_struct()
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


def read_assets(slepok: str, site_type: str) -> dict:
    """Агрегат УНИКАЛЬНЫХ уточнений (callouts) и библиотечных минус-слов (minus_shared) по ВСЕМ
    (tp,ct) слепка × тип сайта. Read-only (вызывается синхронно из web). Не падает без файлов."""
    slepok = (slepok or "").strip()
    site_type = (site_type or "").strip()
    calls: list[str] = []
    minus_shared: list[str] = []
    try:
        pairs = _iter_tp_ct(slepok, site_type)
    except Exception:  # noqa: BLE001
        pairs = []
    for tp, ct in pairs:
        try:
            calls.extend(kp.read_callouts(site_type, tp, ct, slepok))
        except Exception:  # noqa: BLE001
            pass
        try:
            kd = os.path.join(kp._ct_dir(site_type, tp, ct), "keywords")
            minus_shared.extend(kp._read_lines(os.path.join(kd, f"{slepok}_minus_shared.txt")))
        except Exception:  # noqa: BLE001
            pass
    return {"callouts": kp._dedup(calls), "minus_shared": kp._dedup(minus_shared), "pairs": len(pairs)}


def apply_save_assets(spec: dict, actor: str = "") -> dict:
    """spec: {slepok, site_type, callouts?:[...], minus_shared?:[...]}.
    ВЕЕРОМ пишет переданный набор во ВСЕ (tp,ct) слепка × тип сайта (dual-write DST+M3):
      • callouts    → callouts/{slepok}.txt   (уровень слепок×тип, продублирован по ct);
      • minus_shared → keywords/{slepok}_minus_shared.txt (библиотека, тоже по ct).
    Пишутся ТОЛЬКО ключи, присланные в spec (отсутствует callouts → уточнения не трогаем;
    отсутствует minus_shared → библиотеку не трогаем → не затираем пустым)."""
    slepok = (spec.get("slepok") or "").strip()
    site_type = (spec.get("site_type") or "").strip()
    if not (slepok and site_type):
        return {"ok": False, "error": "нужны slepok/site_type"}
    has_callouts = "callouts" in spec
    has_minus = "minus_shared" in spec
    if not (has_callouts or has_minus):
        return {"ok": False, "error": "нечего сохранять (нет callouts/minus_shared)"}
    calls: list[str] = []
    minus_shared: list[str] = []
    errs: list[str] = []
    if has_callouts:
        calls, e1 = _clean_kw_lines(spec.get("callouts"), minus=False)
        errs += e1
    if has_minus:
        minus_shared, e2 = _clean_kw_lines(spec.get("minus_shared"), minus=True)
        errs += e2
    if errs:
        return {"ok": False, "error": "валидация: " + "; ".join(errs[:10])}
    with _EDIT_LOCK:
        pairs = _iter_tp_ct(slepok, site_type)
        if not pairs:
            return {"ok": False, "error": f"нет (tp,ct) в структуре для {slepok}/{site_type}"}
        write: dict = {}
        ok = True
        if has_callouts:
            rels = [_pack_rel_callouts(site_type, tp, ct, f"{slepok}.txt") for tp, ct in pairs]
            r = _dual_write_pack_files_same(rels, "\n".join(calls) + ("\n" if calls else ""))
            write["callouts"] = r
            ok = ok and bool(r["ok"])
        if has_minus:
            rels = [_pack_rel(site_type, tp, ct, f"{slepok}_minus_shared.txt") for tp, ct in pairs]
            r = _dual_write_pack_files_same(rels, "\n".join(minus_shared) + ("\n" if minus_shared else ""))
            write["minus_shared"] = r
            ok = ok and bool(r["ok"])
        result = {"ok": ok, "pairs": len(pairs),
                  "callouts_rows": (len(calls) if has_callouts else None),
                  "minus_shared_rows": (len(minus_shared) if has_minus else None),
                  "write": write}
    _audit("save_assets", actor,
           {"slepok": slepok, "site_type": site_type, "pairs": len(pairs),
            "callouts_rows": (len(calls) if has_callouts else None),
            "minus_shared_rows": (len(minus_shared) if has_minus else None)}, result)
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
            "tp5": "Поиск", "tp6": "МК", "tp7": "Товарка"}.get(tp, tp)
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
        "edit_callouts": apply_edit_callouts,
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

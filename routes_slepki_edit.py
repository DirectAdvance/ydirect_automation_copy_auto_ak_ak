"""Роуты редактора структуры/ключей слепков (вкладка «Структура слепков» → редактируемая).

Все правки — АДМИН-ONLY (архитектура F) и идут в ОБЩУЮ очередь создания РК как edit-джобы
(_kind ∈ slepki_editor._EDIT_KINDS) через job_new → воркер применяет СЕРИЙНО (slepki_editor.handle_job).
UI не применяет правку сразу: enqueue + показ статуса в очереди (та же карточка, что у create-джоб).

Read-only просмотр ключей (GET /keywords) — синхронно в web-процессе (чтение пака, без записи).
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Callable

from flask import Response, jsonify, request, session


# ── экспорт структуры слепка в .xlsx (чистый stdlib OOXML, БЕЗ openpyxl) ──────────────────────
# Генерируем минимальный валидный xlsx: zipfile + inline-strings. Открывается в Excel без
# предупреждения о повреждении. XML-escape (& < > "), UTF-8 (кириллица в данных).

def _xlsx_xesc(s) -> str:
    """XML-escape для текста ячейки/атрибута: & < > " (порядок важен — & первым)."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xlsx_col(n: int) -> str:
    """1-based номер колонки → буквенный адрес (1→A, 27→AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_sheet_name(name: str) -> str:
    """Имя листа Excel: ≤31 символа, без : \\ / ? * [ ]."""
    nm = re.sub(r'[:\\/?*\[\]]', " ", str(name or "Лист1")).strip() or "Лист1"
    return nm[:31]


def _xlsx_bytes(sheet_name: str, headers: list, rows: list) -> bytes:
    """Собрать .xlsx (один лист) из stdlib. headers — список заголовков колонок,
    rows — список списков ячеек (строки). Все значения — inline-strings. Возвращает bytes."""
    def _row_xml(r_idx: int, cells: list) -> str:
        out = [f'<row r="{r_idx}">']
        for c_idx, val in enumerate(cells, start=1):
            ref = f"{_xlsx_col(c_idx)}{r_idx}"
            out.append(
                f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f'{_xlsx_xesc(val)}</t></is></c>'
            )
        out.append("</row>")
        return "".join(out)

    body = [_row_xml(1, headers)]
    for i, row in enumerate(rows, start=2):
        body.append(_row_xml(i, row))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>" + "".join(body) + "</sheetData></worksheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xlsx_xesc(_xlsx_sheet_name(sheet_name))}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


# ── парсинг таргетинга/сегментов для экспорта (эквивалент index.html) ──────────────────────────
_EX_CT_RE = re.compile(r"ct\d{4}")
_EX_STATE_RE = re.compile(r"_(aon|aoff)_")
_EX_AUD_CTS = {"ct0002", "ct0003", "ct0004", "ct0005"}
# tp6/tp7 — метка из хвоста имени (эквивалент _tp67Targeting в index.html)
_EX_KS_AUD = re.compile(r"кс\s*\+\s*аудитори|аудитори\s*\+\s*кс")
_EX_AUTOT = re.compile(r"автотаргет")
_EX_AUD = re.compile(r"аудитори")
_EX_KS = re.compile(r"(^|[^0-9a-zа-яё])кс([^0-9a-zа-яё]|$)|ключев")
_EX_AT = re.compile(r"\bat\b")
_EX_MANUAL = re.compile(r"manual")
# запечённая метка tp.tgt → полная форма (эквивалент _TGT_BADGE_MAP)
_EX_TGT_LABELS = {
    "автотаргетинг": "автотаргетинг", "КС": "КС", "КС+авто": "КС+автотаргетинг",
    "аудитории": "аудитории", "ауд+авто": "аудитории+автотаргетинг",
    "КС+аудитории": "КС+аудитории", "КС+ауд+авто": "КС+аудитории+автотаргетинг",
}


def _ex_first_ct(gc: str) -> str:
    m = _EX_CT_RE.search(gc or "")
    return m.group(0) if m else ""


def _ex_state(gc: str) -> str:
    m = _EX_STATE_RE.search(gc or "")
    return m.group(1) if m else ""


def _ex_strip_prefix(t: str) -> str:
    return re.sub(r"^РСЯ\s+", "", re.sub(r"^Поиск\s+", "", str(t or "")))


def _ex_tp_num(code: str) -> int:
    m = re.search(r"\d+", str(code or ""))
    return int(m.group(0)) if m else 0


def _ex_tgt_from(aud: bool, state: str) -> str:
    """Эквивалент _tgtFrom(aud, state) из index.html."""
    if aud:
        return "КС+аудитории" if state == "aoff" else "аудитории"
    return "КС" if state == "aoff" else "автотаргетинг"


def _ex_seg_tgt(tpn: int, gc: str, baked: str | None) -> str:
    """Таргетинг сегментной группы (эквивалент _grpTargeting/_tgtFrom + override tp2/tp4)."""
    if baked:
        return baked
    if tpn == 3:
        return "Товарная галерея"
    aud = _ex_first_ct(gc) in _EX_AUD_CTS
    lbl = _ex_tgt_from(aud, _ex_state(gc))
    if tpn in (2, 4) and lbl == "автотаргетинг":  # tp2/tp4 aon = КС, не автотаргет
        lbl = "КС"
    return lbl


def _ex_tp67_tgt(it: dict) -> str:
    """Метка таргетинга tp6/tp7 из имени (эквивалент _tp67Targeting)."""
    t = str(it.get("t") or "").lower()
    if _EX_KS_AUD.search(t):
        return "КС+аудитории"
    if _EX_AUTOT.search(t):
        return "автотаргетинг"
    if _EX_AUD.search(t):
        return "аудитории"
    if _EX_KS.search(t):
        return "КС"
    if _EX_AT.search(t):
        return "автотаргетинг"
    if _EX_MANUAL.search(t):
        return "КС"
    st = _ex_state(it.get("gc") or "") or _ex_state(it.get("c") or "")
    return "КС" if st == "aoff" else "автотаргетинг"


def _ex_iter_items(t: dict):
    """(split_label, item) по всем контейнерам tp: плоские groups + splits.groups."""
    for g in (t.get("groups") or []):
        for it in (g.get("items") or []):
            if isinstance(it, dict):
                yield "", it
    for sp in (t.get("splits") or []):
        lbl = sp.get("label") or ""
        for g in (sp.get("groups") or []):
            for it in (g.get("items") or []):
                if isinstance(it, dict):
                    yield lbl, it


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _ascii_slug(s: str) -> str:
    """Транслит + очистка для ASCII-имени файла (кириллица → латиница, прочее → _)."""
    out = []
    for ch in str(s or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        else:
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    return slug or "export"


def _build_export_rows(slepki_editor, slepok: str, site_type: str) -> list:
    """Строки экспорта структуры слепка × типа сайта. Группировка кампаний = как в UI
    (index.html _campaignize / slepkiTpTree). Возвращает список списков (7 колонок)."""
    from . import blueprint_targeting as _btg  # ct→сегмент (leaf-модуль, без цикла)

    struct = slepki_editor._load_struct(mutable=False)   # read-only обход → копия не нужна
    d = slepki_editor._find_dir(struct, slepok)
    s = slepki_editor._find_site(d, site_type) if d else None
    if not s:
        return []
    try:
        seg_map = _btg._ct_segment_map()
    except Exception:  # noqa: BLE001
        seg_map = {}
    try:
        non_auto = set(_btg._non_auto_slepki())
    except Exception:  # noqa: BLE001
        non_auto = set()

    def _seg_of(gc: str) -> str:
        return seg_map.get(_ex_first_ct(gc), "Марки")

    rows: list = []
    for t in (s.get("tp") or []):
        code = (t.get("code") or "").strip()
        title = (t.get("title") or "").strip()
        tpn = _ex_tp_num(code)
        baked = _EX_TGT_LABELS.get((t.get("tgt") or "").strip()) if (t.get("tgt") or "").strip() else None

        if tpn in (6, 7):
            # каждая группа = своя кампания (имя из item.t); таргетинг из хвоста имени
            for _lbl, it in _ex_iter_items(t):
                nm = _ex_strip_prefix(it.get("t") or "")
                camp = it.get("t") or nm
                rows.append([site_type, code, title, camp, camp,
                             _ex_tp67_tgt(it), it.get("gc") or ""])
            continue

        # сегментные tp1/2/4/5 + tp3: кампании = item.camp_names (иначе split-label/сегмент по gc)
        seg_tp = (tpn in (1, 2, 4, 5)) and not (tpn == 2 and slepok in non_auto)
        seen: set = set()
        for split_label, it in _ex_iter_items(t):
            gc = it.get("gc") or ""
            if not gc:
                continue
            nm = _ex_strip_prefix(it.get("t") or "")
            grp = nm or gc
            cn_list = it.get("camp_names") or []
            if not cn_list:
                if split_label:
                    cn_list = [split_label]
                elif seg_tp:
                    cn_list = [_seg_of(gc)]
                else:
                    cn_list = [nm or "Общее"]
            tgt = _ex_seg_tgt(tpn, gc, baked)
            for cn0 in cn_list:
                cn = cn0 or "Общее"
                key = (code, cn, grp)
                if key in seen:
                    continue
                seen.add(key)
                rows.append([site_type, code, title, cn, grp, tgt, gc])
    return rows


# Компонент-энумы кодера для мастера add-ct (СЫРОЙ gc не вводится — только сборка из этих значений).
# Значения — реальный корпус из slepki_structure.json (aon/aoff, форматы, возраст, пол, частые регионы).
_MODE_OPTS = [{"code": "aon", "name": "Автотаргет (aon)"}, {"code": "aoff", "name": "КС (aoff)"}]
_FMT_OPTS = [
    {"code": "ct001", "name": "ТГО (только TextAd)"},
    {"code": "ct010", "name": "Комбинированный (TextAd+Listing+Shopping)"},
    {"code": "ct009", "name": "Товарное (Shopping+Listing без TextAd)"},
    {"code": "ct003", "name": "Товарное/Фид"},
]
_AGE_OPTS = [{"code": "ag011", "name": "24-55+"}, {"code": "ag001", "name": "Все"}]
_GENDER_OPTS = [{"code": "g00", "name": "Все"}, {"code": "g01", "name": "Мужчины"}, {"code": "g02", "name": "Женщины"}]
_INTEREST_OPTS = [{"code": "n000", "name": "Без интереса"}]
# Регион в структуре — плейсхолдер r0000 (на боевом → реальный по городу аккаунта).
_REGION_OPTS = [
    {"code": "r0000", "name": "Плейсхолдер (по городу на боевом)"},
    {"code": "r0100", "name": "Кемеровская обл."},
    {"code": "r0002", "name": "регион 0002"},
]


def register_slepki_edit_routes(
    bp,
    access,
    *,
    slepki_editor,
    job_new: Callable,
    ag_part1_map: Callable[[], dict],
) -> None:
    def _admin() -> bool:
        return bool(session.get("is_admin"))

    def _bad_scope(slepok: str, site_type: str = "", tp: str = ""):
        """Белый список slepok/site_type/tp по реальной структуре ДО того, как значения попадут
        в путь пак-файла (иначе `?site_type=../../../..` читал/писал любой файл на хосте —
        процессы слепков идут без `User=`, т.е. от root). → None если ок, иначе (json, 400)."""
        err = slepki_editor.validate_scope(slepok, site_type, tp)
        if err:
            return jsonify({"error": err}), 400
        return None

    def _enqueue(kind: str, spec: dict):
        """Поставить edit-джобу в ОБЩУЮ очередь (web-роль: уходит в БД, воркер применит серийно)."""
        body = {"_kind": kind, "spec": spec,
                "_actor": session.get("username") or session.get("login") or "admin"}
        jid = job_new(1, "slepki-edit", body, dict(session), False, True)  # priority=True (правка быстрее наборов)
        return jsonify({"queued": True, "job_id": jid, "kind": kind})

    # ── просмотр ключей группы (read-only, синхронно, БЕЗ admin-гейта) ─────────
    @bp.route("/api/slepki/keywords", methods=["GET"])
    @access
    def slepki_keywords():
        slepok = (request.args.get("slepok") or "").strip()
        site_type = (request.args.get("site_type") or "").strip()
        tp = (request.args.get("tp") or "").strip()
        ct = (request.args.get("ct") or "").strip()
        # group (=gk группы) → per-group файл {slepok}__{gk}.txt (фикс «одинаковые ключи по группам»:
        # без него читался ct-агрегат и все группы одного ct показывали одинаковые ключи). Пусто → легаси-агрегат.
        group = (request.args.get("group") or request.args.get("gk") or "").strip()
        # position (опц.) — имя позиции/кампании строки (tp6/tp7). Нужно фолбэку реальных ключей:
        # создание матчит набор по ct И по позиции (create_set_context._tp67_keywords_from_real_library),
        # поэтому без него карточка могла бы взять не тот набор, что уедет в кабинет.
        position = (request.args.get("position") or "").strip()
        if not (slepok and site_type and tp and ct):
            return jsonify({"error": "нужны slepok/site_type/tp/ct"}), 400
        bad = _bad_scope(slepok, site_type, tp)
        if bad:
            return bad
        data = slepki_editor.read_group_keywords(site_type, tp, ct, slepok, group=group, position=position)
        return jsonify({"slepok": slepok, "site_type": site_type, "tp": tp, "ct": ct, "group": group, **data})

    # ── ассеты слепка: агрегат уточнений + библиотечных минусов (read-only) ────
    @bp.route("/api/slepki/assets", methods=["GET"])
    @access
    def slepki_assets():
        slepok = (request.args.get("slepok") or "").strip()
        site_type = (request.args.get("site_type") or "").strip()
        if not (slepok and site_type):
            return jsonify({"error": "нужны slepok/site_type"}), 400
        bad = _bad_scope(slepok, site_type)
        if bad:
            return bad
        data = slepki_editor.read_assets(slepok, site_type)
        return jsonify({"slepok": slepok, "site_type": site_type, **data})

    # ── именованные наборы минус-слов слепка × тип сайта (read-only) ───────────
    @bp.route("/api/slepki/minus_sets", methods=["GET"])
    @access
    def slepki_minus_sets():
        slepok = (request.args.get("slepok") or "").strip()
        site_type = (request.args.get("site_type") or "").strip()
        if not (slepok and site_type):
            return jsonify({"error": "нужны slepok/site_type"}), 400
        bad = _bad_scope(slepok, site_type)
        if bad:
            return bad
        data = slepki_editor.read_minus_sets(slepok, site_type)
        return jsonify({"slepok": slepok, "site_type": site_type, **data})

    @bp.route("/api/slepki/save_minus_sets", methods=["POST"])
    @access
    def slepki_save_minus_sets():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        slepok = (b.get("slepok") or "").strip()
        site_type = (b.get("site_type") or "").strip()
        if not (slepok and site_type):
            return jsonify({"error": "нужны slepok/site_type"}), 400
        if not isinstance(b.get("sets"), list):
            return jsonify({"error": "нужен список sets"}), 400
        bad = _bad_scope(slepok, site_type)
        if bad:
            return bad
        spec = {"slepok": slepok, "site_type": site_type, "sets": b.get("sets")}
        return _enqueue("save_minus_sets", spec)

    # ── экспорт структуры слепка × типа сайта в .xlsx (read-only, БЕЗ admin-гейта) ─────
    @bp.route("/api/slepki/export_xlsx", methods=["GET"])
    @access
    def slepki_export_xlsx():
        slepok = (request.args.get("slepok") or "").strip()
        site_type = (request.args.get("site_type") or "").strip()
        if not (slepok and site_type):
            return jsonify({"error": "нужны slepok/site_type"}), 400
        bad = _bad_scope(slepok, site_type)
        if bad:
            return bad
        rows = _build_export_rows(slepki_editor, slepok, site_type)
        headers = ["Тип сайта", "tp (код)", "Тип кампании", "Кампания",
                   "Группа", "Таргетинг", "Кодер (gc)"]
        sheet = _xlsx_sheet_name(f"{slepok} {site_type}")
        data = _xlsx_bytes(sheet, headers, rows)
        fname = f"slepok_{_ascii_slug(slepok)}_{_ascii_slug(site_type)}.xlsx"
        return Response(
            data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @bp.route("/api/slepki/save_assets", methods=["POST"])
    @access
    def slepki_save_assets():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip())
        if bad:
            return bad
        spec = {"slepok": b.get("slepok"), "site_type": b.get("site_type")}
        # Только присланные ключи веером пишутся во ВСЕ (tp,ct); отсутствующий ключ —
        # соответствующие файлы НЕ трогаются (не затираем библиотеку/уточнения пустым).
        if "callouts" in b:
            spec["callouts"] = b.get("callouts") or []
        if "minus_shared" in b:
            spec["minus_shared"] = b.get("minus_shared") or []
        if "callouts" not in spec and "minus_shared" not in spec:
            return jsonify({"error": "нечего сохранять (нет callouts/minus_shared)"}), 400
        return _enqueue("save_assets", spec)

    # ── компоненты кодера (read-only, БЕЗ admin-гейта: нужны для расшифровки кодера в панели таргетингов) ──
    @bp.route("/api/slepki/coder_components", methods=["GET"])
    @access
    def slepki_coder_components():
        m = ag_part1_map()
        ct_list = sorted(({"code": k, "name": v} for k, v in m.items()), key=lambda x: x["code"])
        return jsonify({
            "ct_list": ct_list,          # ТОЛЬКО зарегистрированные ct кодера (ag_part1)
            "mode": _MODE_OPTS, "fmt": _FMT_OPTS, "age": _AGE_OPTS,
            "gender": _GENDER_OPTS, "interest": _INTEREST_OPTS, "region": _REGION_OPTS,
        })

    # ── правки → очередь (admin-only) ─────────────────────────────────────────
    @bp.route("/api/slepki/edit_keywords", methods=["POST"])
    @access
    def slepki_edit_keywords():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip(),
                         (b.get("tp") or "").strip())
        if bad:
            return bad
        spec = {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "ct": b.get("ct"),
            # group (=gk) → правка пишет per-group файл {slepok}__{gk}.txt (apply_edit_keywords поддерживает).
            # Пусто → легаси ct-агрегат (обратная совместимость для групп без gk).
            "group": (b.get("group") or b.get("gk") or ""),
            "positive": b.get("positive") or [], "minus": b.get("minus") or [],
        }
        # Библиотечные минусы — редактируются, только если ключ прислан (иначе файл не трогаем).
        # SCOPE строго этот (slepok, site_type, tp, ct); имя файла с префиксом {slepok}_ →
        # чужие слепки физически недостижимы (см. slepki_editor.apply_edit_keywords).
        if "minus_shared" in b:
            spec["minus_shared"] = b.get("minus_shared") or []
        return _enqueue("edit_keywords", spec)

    # POST /api/slepki/edit_callouts удалён 2026-07-19 как мёртвый: вызывающих не осталось —
    # уточнения сохраняются веером через save_assets (slepki_ui.js `_slAssetsSave`).

    @bp.route("/api/slepki/toggle_aon_aoff", methods=["POST"])
    @access
    def slepki_toggle_aon_aoff():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip(),
                         (b.get("tp") or "").strip())
        if bad:
            return bad
        return _enqueue("toggle_aon_aoff", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "segment": b.get("segment"), "mode": b.get("mode"),
        })

    @bp.route("/api/slepki/add_ct_group", methods=["POST"])
    @access
    def slepki_add_ct_group():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip(),
                         (b.get("tp") or "").strip())
        if bad:
            return bad
        return _enqueue("add_ct_group", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"), "tp": b.get("tp"),
            "ct": b.get("ct"), "mode": b.get("mode") or "aon",
            "region": b.get("region") or "r0000", "fmt": b.get("fmt") or "ct001",
            "age": b.get("age") or "ag011", "gender": b.get("gender") or "g00",
            "interest": b.get("interest") or "n000", "desc": b.get("desc") or "",
        })

    @bp.route("/api/slepki/remove_ct_group", methods=["POST"])
    @access
    def slepki_remove_ct_group():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip(),
                         (b.get("tp") or "").strip())
        if bad:
            return bad
        return _enqueue("remove_ct_group", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "gc": b.get("gc"),
        })

    @bp.route("/api/slepki/set_name_override", methods=["POST"])
    @access
    def slepki_set_name_override():
        if not _admin():
            return jsonify({"error": "только администратор"}), 403
        b = request.get_json(silent=True) or {}
        bad = _bad_scope((b.get("slepok") or "").strip(), (b.get("site_type") or "").strip(),
                         (b.get("tp") or "").strip())
        if bad:
            return bad
        return _enqueue("set_name_override", {
            "slepok": b.get("slepok"), "site_type": b.get("site_type"),
            "tp": b.get("tp"), "segment": b.get("segment"), "mode": b.get("mode") or "",
            "name_override": b.get("name_override") or "",
        })

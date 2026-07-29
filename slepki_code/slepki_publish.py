"""Публикация слепка: контент и структура появляются ВМЕСТЕ.

Проблема, которую этот модуль закрывает. Структуру страница `/direct/automation/slepki` видит
сразу (`slepki_store.assemble()` пересобирает кэш по mtime+size part-файла), а контент пака живёт
на M3 и доезжает до LXC 101 ночным синком `sync_content_m3.py` (00:00/12:00). Записать одно без
другого — значит показать Семёну слепок с пустыми ключами/уточнениями и ждать до ночи.

Решение — переиспользовать dual-write редактора слепков (`slepki_editor._dual_write_pack_files_map`):
он пишет каждый файл И в локальное зеркало `/opt/neuro_content_local` (его читает приложение —
видно сразу), И в M3-источник (переживёт ночной синк и orphan-cleanup). Синк не нужен вовсе.

Порядок здесь принципиален: **сначала контент, потом структура**. Структура — это то, что делает
слепок видимым; если записать её первой, между двумя записями существует окно, в котором слепок
уже в UI, а ключей у него ещё нет. Обратный порядок безопасен: неприкаянные файлы пака никому не
видны, пока на них не сошлётся структура.

Использование (воркер бота, после кнопки «Применить»):

    from .slepki_publish import publish
    rep = publish(entry, pack_files)      # entry — запись директолога, pack_files — {rel: text}
    if not rep["ok"]: ...                 # структура НЕ записана: контент не доехал
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import slepki_editor as se
from . import slepki_store

log = logging.getLogger(__name__)


def _verify_local(mapping: dict[str, str]) -> list[dict]:
    """Перечитать записанные файлы зеркала и сравнить с тем, что писали.

    Отчёт dual-write говорит «записал», а гарантия нужна фактическая: битый монт, кончившееся
    место и обрезанная запись выглядят как успех на уровне вызова.
    """
    bad: list[dict] = []
    for rel, text in mapping.items():
        p = Path(se._dst_abs(rel))
        try:
            got = p.read_text(encoding="utf-8")
        except OSError as e:
            bad.append({"rel": rel, "error": f"не читается после записи: {e}"})
            continue
        if got != text:
            bad.append({"rel": rel,
                        "error": f"содержимое разошлось: ждали {len(text)} симв., на диске {len(got)}"})
    return bad


def publish(entry: dict | None, pack_files: dict[str, str] | None = None) -> dict:
    """Опубликовать слепок целиком: контент пака + запись структуры.

    entry      — запись директолога (`{"key": ..., "site_types": [...]}`) либо None, если правится
                 только контент существующего слепка.
    pack_files — `{относительный_путь_в_паке: текст}`; пусто → публикуется только структура.

    Возвращает отчёт с числами (для сообщения в бот) и `ok`. При провале контента структура
    НЕ записывается — слепок не появится в UI полупустым.
    """
    mapping = dict(pack_files or {})
    rep: dict = {"ok": False, "content": None, "structure": None,
                 "slepok": (entry or {}).get("key")}

    if mapping:
        res = se._dual_write_pack_files_map(mapping)
        bad = _verify_local(mapping) if res.get("dst_ok") else []
        res["verify_failed"] = bad
        rep["content"] = res
        if not res.get("ok") or bad:
            rep["error"] = ("контент не доехал целиком — структуру не пишу, "
                            "иначе слепок появится в UI без ключей")
            log.error("publish %s: контент не доехал: %s", rep["slepok"], res)
            return rep

    if entry is not None:
        dirs = [d for d in slepki_store.assemble().get("directologists", [])
                if d.get("key") != entry.get("key")]
        dirs.append(entry)
        changed = slepki_store.write_directologists(dirs)
        rep["structure"] = {"written": changed, "total": len(dirs)}

    rep["ok"] = True
    log.info("publish %s: файлов пака %d, структура %s",
             rep["slepok"], len(mapping), (rep["structure"] or {}).get("written"))
    return rep


def summary(rep: dict) -> str:
    """Однострочная сводка для сообщения в Telegram — числами, а не «успешно»."""
    if not rep.get("ok"):
        return f"❌ {rep.get('error') or 'публикация не прошла'}"
    c = rep.get("content") or {}
    s = rep.get("structure") or {}
    parts = []
    if c:
        parts.append(f"контент: {c.get('dst_ok', 0)}/{c.get('count', 0)} файлов в зеркале"
                     + (", M3 ok" if c.get("m3_ok") else f", ⚠️ M3: {c.get('m3_error')}"))
    if s:
        parts.append(f"структура: переписано {len(s.get('written') or [])} part-файл(ов)")
    return "✅ " + "; ".join(parts or ["изменений нет"])

#!/usr/bin/env python3
"""Scoped git exporter для сервиса копирования кабинетов (/direct/automation/copy).

Зона copy живёт в ОТДЕЛЬНОМ репозитории ydirect_automation_copy_auto_ak_ak — так же, как
контент-редактор в yandex_direct_content_redactor (tools/content_redactor_git.py).
Исходник правды — рабочая копия home/seoadvanced; репозиторий наполняется экспортом.

Команды: scope | export | status
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]          # home/seoadvanced
REMOTE_URL = "https://github.com/DirectAdvance/ydirect_automation_copy_auto_ak_ak.git"
EXPORT_ROOT = Path.home() / ".cache" / "seoadvanced_git_exports" / "ydirect_automation_copy_auto_ak_ak"
BRANCH = "main"

INCLUDE_GLOBS = (
    # Код сервиса копирования.
    "direct/copy_service/*.py",
    "direct/copy_main.py",
    "direct/web/routes_copy.py",
    # Документация зоны.
    "direct/COPY_INDEX.md",
    "direct/COPY_README.md",
    "direct/STATE_COPY_OTHER.md",
    # Тесты зоны.
    "direct/tests/test_copy_integration_guards.py",
    "direct/tests/test_direct_copy_transient_retry.py",
    # UI вкладки /direct/automation/copy.
    "templates/direct/copy.html",
    "templates/direct/copy_other.html",
    "templates/direct/_copy_common.html",
    "static/direct/copy_*.js",
    "static/direct/copy_*.css",
    "static/direct/content_copy.js",
    # Git-контракт зоны.
    "direct/tools/direct_git_guard.py",
    "direct/tools/copy_service_git.py",
)

# ⛔ Ничего из этих зон в copy-репозиторий попадать не должно.
DENY_GLOBS = (
    "direct/content/*",
    "direct/create/*",
    "direct/slepki_code/*",
    "direct/slepki/*",
)


def _run(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def scope_files() -> list[str]:
    out: set[str] = set()
    for pattern in INCLUDE_GLOBS:
        for path in SOURCE_ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(SOURCE_ROOT).as_posix()
            if any(Path(rel).match(d) for d in DENY_GLOBS):
                continue
            out.add(rel)
    return sorted(out)


def export(push: bool = True) -> dict:
    files = scope_files()
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    if not (EXPORT_ROOT / ".git").exists():
        _run(["git", "init", "-b", BRANCH], cwd=EXPORT_ROOT)
        _run(["git", "remote", "add", "origin", REMOTE_URL], cwd=EXPORT_ROOT)
    # чистим всё, кроме .git — экспорт зеркалит scope один-в-один
    for child in EXPORT_ROOT.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for rel in files:
        dst = EXPORT_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_ROOT / rel, dst)
    (EXPORT_ROOT / "README.md").write_text(
        "# ydirect_automation_copy_auto_ak_ak\n\n"
        "Сервис копирования кабинетов Яндекс.Директа 1:1 — вкладка `/direct/automation/copy`.\n\n"
        "⚠️ Репозиторий наполняется ЭКСПОРТОМ из рабочей копии `home/seoadvanced`,\n"
        "править надо там, а не здесь: `direct/tools/copy_service_git.py export`.\n\n"
        f"Файлов в зоне: {len(files)}. Юнит: `direct-copy.service` (:5022), "
        "точка входа `python3 -m direct.copy_main`.\n",
        encoding="utf-8")
    _run(["git", "add", "-A"], cwd=EXPORT_ROOT)
    st = _run(["git", "status", "--porcelain"], cwd=EXPORT_ROOT)
    changed = bool(st.stdout.strip())
    if changed:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _run(["git", "commit", "-q", "-m", f"sync copy service {stamp}"], cwd=EXPORT_ROOT)
    head = _run(["git", "rev-parse", "HEAD"], cwd=EXPORT_ROOT).stdout.strip()
    if push:
        _run(["git", "push", "-q", "-u", "origin", f"HEAD:refs/heads/{BRANCH}"], cwd=EXPORT_ROOT)
    return {"ok": True, "files_count": len(files), "changed": changed,
            "head": head, "remote": REMOTE_URL, "export_repo": str(EXPORT_ROOT)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["scope", "export", "status"])
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scope":
        print(json.dumps({"files": scope_files(), "count": len(scope_files())},
                         ensure_ascii=False, indent=2))
    elif a.cmd == "export":
        print(json.dumps(export(push=not a.no_push), ensure_ascii=False))
    else:
        print(json.dumps({"export_repo": str(EXPORT_ROOT), "exists": EXPORT_ROOT.exists(),
                          "files_in_scope": len(scope_files())}, ensure_ascii=False))


if __name__ == "__main__":
    main()

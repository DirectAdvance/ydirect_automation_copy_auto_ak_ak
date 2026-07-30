#!/usr/bin/env python3
"""Canonical URL -> Git zone map for Direct automation.

The working tree is physically shared under home/seoadvanced, but changes must
be committed/exported by URL zone:
  copy       -> DirectAdvance/ydirect_automation_copy_auto_ak_ak
  content    -> DirectAdvance/yandex_direct_content_redactor
  slepki     -> DirectAdvance/slepki_direktologov
  automation -> DirectAdvance/neurodirectologist
"""
from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]  # home/seoadvanced

REMOTE_BY_ZONE = {
    "copy": "https://github.com/DirectAdvance/ydirect_automation_copy_auto_ak_ak.git",
    "content": "https://github.com/DirectAdvance/yandex_direct_content_redactor.git",
    "slepki": "https://github.com/DirectAdvance/slepki_direktologov.git",
    "automation": "https://github.com/DirectAdvance/neurodirectologist.git",
}

URL_BY_ZONE = {
    "copy": "/direct/automation/copy",
    "content": "/direct/automation/content",
    "slepki": "/direct/automation/slepki",
    "automation": "/direct/automation",
}

GLOBAL_DENY_GLOBS = (
    "*/.git/*",
    "*/.claude/*",
    "*/.venv/*",
    "*/.pytest_cache/*",
    "*/.ruff_cache/*",
    "*/__pycache__/*",
    "*/._*",
    "*/.DS_Store",
    "*.bak",
    "*.db",
    "*.log",
    "*.pyc",
    "*.editbak.*",
    "direct/scratchpad/*",
    "direct/reconciler_staging/*",
    "direct/var/*",
)

SHARED_GLOBS = (
    "direct/CLAUDE.md",
    "direct/INDEX.md",
    "direct/README.md",
    "direct/STATE.md",
    "direct/ERRORS_JOURNAL.md",
    "direct/agent_board_bridge.py",
    "direct/core/direct_repository.py",
    "direct/tools/direct_git_guard.py",
    "direct/tools/direct_git_zones.py",
)

COPY_GLOBS = (
    "direct/COPY_INDEX.md",
    "direct/COPY_README.md",
    "direct/STATE_COPY_OTHER.md",
    "direct/copy_main.py",
    "direct/copy_service/*.py",
    "direct/web/routes_copy.py",
    "direct/tests/test_copy*.py",
    "direct/tests/test_direct_copy_transient_retry.py",
    "direct/tools/copy_service_git.py",
    "templates/direct/_copy_common.html",
    "templates/direct/copy*.html",
    "static/direct/copy*",
    "static/direct/content_copy.js",
)

CONTENT_GLOBS = (
    "direct/CONTENT_EDITOR*.md",
    "direct/account*.py",
    "direct/accounts*.py",
    "direct/content*.py",
    "direct/content/*.py",
    "direct/price_check*.py",
    "direct/link_check.py",
    "direct/model_url_config.json",
    "direct/model_urls.py",
    "direct/web/routes_accounts.py",
    "direct/web/routes_content*.py",
    "direct/web/routes_overview.py",
    "direct/tests/test_content*.py",
    "direct/tests/test_price_check*.py",
    "direct/tests/test_routes.py",
    "direct/tools/content_redactor_git.py",
    "templates/direct/accounts.html",
    "templates/direct/content*.html",
    "static/direct/accounts_ui.*",
    "static/direct/automation_content.js",
    "static/direct/content*.js",
    "static/direct/content_editor.css",
)

SLEPKI_GLOBS = (
    "direct/SLEPKI*.md",
    "direct/STRUCTURE_EXCLUSIONS.md",
    "direct/slepki/*",
    "direct/slepki_code/*.py",
    "direct/slepki_bot.py",
    "direct/slepki_main.py",
    "direct/slepki_worker_main.py",
    "direct/scripts/slepki_preflight.py",
    "direct/web/routes_slepki*.py",
    "direct/tests/test_slepki*.py",
    "direct/deploy/direct-slepki*.service",
    "templates/direct/slepki.html",
    "static/direct/slepki_*",
)

ZONE_GLOBS = {
    "copy": COPY_GLOBS,
    "content": CONTENT_GLOBS,
    "slepki": SLEPKI_GLOBS,
}


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def source_rel(path: str | Path) -> str:
    value = Path(path)
    if value.is_absolute():
        return value.resolve().relative_to(SOURCE_ROOT).as_posix()
    rel = value.as_posix().lstrip("./")
    if rel.startswith(("direct/", "static/", "templates/", "agent_board/")):
        return rel
    return f"direct/{rel}"


def classify(path: str | Path) -> str:
    rel = source_rel(path)
    if _match_any(rel, GLOBAL_DENY_GLOBS):
        return "ignored"
    if _match_any(rel, SHARED_GLOBS):
        return "shared"
    hits = [zone for zone, patterns in ZONE_GLOBS.items() if _match_any(rel, patterns)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "ambiguous"
    if rel.startswith(("direct/", "static/direct/", "templates/direct/", "agent_board/")):
        return "automation"
    return "outside"


def scope_files(zone: str) -> list[str]:
    if zone == "automation":
        files = []
        for root in ("direct", "static/direct", "templates/direct", "agent_board"):
            base = SOURCE_ROOT / root
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(SOURCE_ROOT).as_posix()
                if classify(rel) == "automation":
                    files.append(rel)
        return sorted(set(files))
    if zone == "shared":
        patterns = SHARED_GLOBS
    else:
        patterns = ZONE_GLOBS[zone]
    out: set[str] = set()
    for pattern in patterns:
        for path in SOURCE_ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(SOURCE_ROOT).as_posix()
            if classify(rel) == zone:
                out.add(rel)
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    classify_cmd = sub.add_parser("classify")
    classify_cmd.add_argument("path", nargs="+")
    scope_cmd = sub.add_parser("scope")
    scope_cmd.add_argument("zone", choices=("copy", "content", "slepki", "automation", "shared"))
    args = parser.parse_args()

    if args.cmd == "classify":
        print(json.dumps({path: classify(path) for path in args.path}, ensure_ascii=False, indent=2))
    else:
        files = scope_files(args.zone)
        print(json.dumps({
            "zone": args.zone,
            "url": URL_BY_ZONE.get(args.zone),
            "remote": REMOTE_BY_ZONE.get(args.zone),
            "count": len(files),
            "files": files,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

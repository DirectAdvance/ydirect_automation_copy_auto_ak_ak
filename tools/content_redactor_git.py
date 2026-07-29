#!/usr/bin/env python3
"""Scoped git exporter for Direct content/accounts editor files.

The running service is split across two project roots:
  - home/seoadvanced/direct/...
  - home/seoadvanced/templates/direct and static/direct

The copy service already has its own nested git contract. This helper keeps the
content/accounts editor in a separate GitHub repository without mixing copy
files into that repository.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]  # home/seoadvanced
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
DEFAULT_EXPORT_REPO = CACHE_ROOT / "seoadvanced_git_exports" / "yandex_direct_content_redactor"
REMOTE_URL = "https://github.com/DirectAdvance/yandex_direct_content_redactor.git"
DEFAULT_BRANCH = "main"
MANIFEST_FILE = ".content_redactor_manifest.json"
TOKEN_ENV_NAMES = ("GIT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

INCLUDE_GLOBS = (
    # Service documentation and shared notes needed by agents touching this scope.
    "direct/CLAUDE.md",
    "direct/CONTENT_EDITOR*.md",
    "direct/UI_MAP.md",
    "direct/INDEX.md",
    "direct/STATE.md",
    "direct/ERRORS_JOURNAL.md",
    "direct/tools/content_redactor_git.py",
    # Content editor and accounts service code.
    "direct/account*.py",
    "direct/accounts*.py",
    "direct/content*.py",
    "direct/routes_accounts.py",
    "direct/routes_content*.py",
    "direct/routes_overview.py",
    "direct/price_check*.py",
    # Shared Direct API/runtime dependencies used by content/accounts tasks.
    "direct/agent_board_bridge.py",
    "direct/campaign.py",
    "direct/direct_repository.py",
    "direct/direct_v501_client.py",
    "direct/gateway_client.py",
    "direct/grid_content_verifier.py",
    "direct/grid_finalize.py",
    "direct/grid_read.py",
    "direct/link_check.py",
    "direct/model_url_config.json",
    "direct/model_urls.py",
    "direct/uac_client.py",
    "direct/uac_read.py",
    "direct/yandex_gateway.py",
    # Relevant tests.
    "direct/tests/test_content*.py",
    "direct/tests/test_routes.py",
    "direct/tests/test_direct_repository.py",
    "direct/tests/test_campaign_cookie_picker.py",
    # UI files for /direct/automation/content and /direct/automation/accounts.
    "templates/direct/accounts.html",
    "templates/direct/content_admin.html",
    "templates/direct/content_editor.html",
    "static/direct/accounts_ui.*",
    "static/direct/automation_content.js",
    "static/direct/content_editor*.js",
    "static/direct/content_editor.css",
    "static/direct/content_renames.js",
    # Agent Board integration for content/accounts task descriptions.
    "agent_board/git_context.py",
    "direct/tests/test_agent_board_git_context.py",
)

COPY_DENY_GLOBS = (
    "direct/COPY*",
    "direct/copy*.py",
    "direct/routes_copy.py",
    "direct/tests/test_copy*.py",
    "templates/direct/_copy_common.html",
    "templates/direct/copy*.html",
    "static/direct/copy*",
    "static/direct/content_copy.js",
)

GLOBAL_DENY_GLOBS = (
    "*/__pycache__/*",
    "*/._*",
    "*/.DS_Store",
    "*.bak",
    "*.db",
    "*.log",
    "*.pyc",
)


class GuardError(RuntimeError):
    pass


def _secret_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip().strip('"').strip("'")
    for parent in SOURCE_ROOT.parents:
        env_path = parent / ".secret" / ".env"
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key.strip() in names:
                return value.strip().strip('"').strip("'")
    return ""


def _git_env() -> dict:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    token = _secret_env_value(TOKEN_ENV_NAMES)
    if token:
        askpass = Path(tempfile.gettempdir()) / "content_redactor_git_askpass.sh"
        if not askpass.exists():
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "*Username*) printf '%s\\n' \"${GIT_USERNAME:-x-access-token}\" ;;\n"
                "*) printf '%s\\n' \"$GIT_TOKEN\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
        env.setdefault("GIT_ASKPASS", str(askpass))
        env.setdefault("GIT_USERNAME", "x-access-token")
        env["GIT_TOKEN"] = token
    return env


def _run(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = _git_env() if args and args[0] == "git" else None
    proc = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True, env=env)
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GuardError(f"{' '.join(args)} failed: {msg}")
    return proc


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_git_state(path: Path) -> dict:
    proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
    if proc.returncode != 0:
        return {}
    repo = Path(proc.stdout.strip())
    return {
        "repo": str(repo),
        "branch": _run(["git", "branch", "--show-current"], cwd=repo, check=False).stdout.strip(),
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo, check=False).stdout.strip(),
    }


def _export_repo(args: argparse.Namespace) -> Path:
    return Path(args.export_repo or os.environ.get("CONTENT_REDACTOR_GIT_PATH") or DEFAULT_EXPORT_REPO).resolve()


def _scope_files() -> list[str]:
    files: set[str] = set()
    for pattern in INCLUDE_GLOBS:
        for path in SOURCE_ROOT.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(SOURCE_ROOT).as_posix()
            if _match_any(rel, GLOBAL_DENY_GLOBS):
                continue
            if _match_any(rel, COPY_DENY_GLOBS):
                continue
            files.add(rel)
    copy_leaks = sorted(rel for rel in files if _match_any(rel, COPY_DENY_GLOBS))
    if copy_leaks:
        raise GuardError(f"copy files are not allowed in content-redactor scope: {copy_leaks}")
    return sorted(files)


def _ensure_repo(repo: Path, branch: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _run(["git", "init", "-b", branch], cwd=repo)
    remote = _run(["git", "remote", "get-url", "origin"], cwd=repo, check=False)
    if remote.returncode != 0:
        _run(["git", "remote", "add", "origin", REMOTE_URL], cwd=repo)
    elif remote.stdout.strip() != REMOTE_URL:
        _run(["git", "remote", "set-url", "origin", REMOTE_URL], cwd=repo)
    current = _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()
    if current != branch:
        _run(["git", "checkout", "-B", branch], cwd=repo)
    _run(
        ["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        cwd=repo,
        check=False,
    )
    if _origin_branch_exists(repo, branch) and not _has_head(repo):
        _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo)


def _origin_branch_exists(repo: Path, branch: str) -> bool:
    proc = _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=repo, check=False)
    return proc.returncode == 0


def _has_head(repo: Path) -> bool:
    return _run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, check=False).returncode == 0


def _ensure_not_behind(repo: Path, branch: str) -> None:
    if not _origin_branch_exists(repo, branch):
        return
    counts = _run(["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"], cwd=repo)
    ahead, behind = [int(x) for x in counts.stdout.strip().split()[:2]]
    if behind:
        if ahead:
            raise GuardError(f"export repo diverged from origin/{branch}: ahead={ahead}, behind={behind}")
        _run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo)


def _porcelain(repo: Path) -> list[str]:
    return [line for line in _run(["git", "status", "--porcelain"], cwd=repo).stdout.splitlines() if line.strip()]


def _manifest_payload(files: list[str]) -> dict:
    return {
        "project": "seoadvanced/direct-content-redactor",
        "remote": REMOTE_URL,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_root": str(SOURCE_ROOT),
        "source_git": {
            "home": _source_git_state(SOURCE_ROOT),
            "direct": _source_git_state(SOURCE_ROOT / "direct"),
        },
        "files": {rel: _sha256(SOURCE_ROOT / rel) for rel in files},
        "copy_scope_excluded": True,
    }


def _read_manifest(repo: Path) -> dict | None:
    path = repo / MANIFEST_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _source_drift(manifest: dict) -> list[str]:
    drift: list[str] = []
    for rel, expected_hash in sorted((manifest.get("files") or {}).items()):
        src = SOURCE_ROOT / rel
        if not src.is_file():
            drift.append(f"missing {rel}")
        elif _sha256(src) != expected_hash:
            drift.append(f"changed {rel}")
    current_scope = set(_scope_files())
    manifest_scope = set((manifest.get("files") or {}).keys())
    for rel in sorted(current_scope - manifest_scope):
        drift.append(f"new {rel}")
    for rel in sorted(manifest_scope - current_scope):
        drift.append(f"removed-from-scope {rel}")
    return drift


def _copy_scope_into_repo(repo: Path, files: list[str], manifest: dict) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for rel in files:
        src = SOURCE_ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n*.log\n*.db\n.DS_Store\n._*\n\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# yandex_direct_content_redactor\n\n"
        "Scoped export of seoadvanced Direct content/accounts editor files.\n\n"
        "Copy-service files are intentionally excluded. Use "
        "`direct/tools/direct_git_guard.py` and branch "
        "`ydirect_automation_copy_auto_ak_ak` for `/direct/automation/copy`.\n",
        encoding="utf-8",
    )
    (repo / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cmd_scope(args: argparse.Namespace) -> int:
    payload = {
        "source_root": str(SOURCE_ROOT),
        "export_repo": str(_export_repo(args)),
        "remote": REMOTE_URL,
        "branch": args.branch,
        "files": _scope_files(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    repo = _export_repo(args)
    _ensure_repo(repo, args.branch)
    if _porcelain(repo):
        raise GuardError(f"export repo has uncommitted changes: {_porcelain(repo)}")
    _ensure_not_behind(repo, args.branch)
    manifest = _read_manifest(repo)
    if not manifest:
        if args.allow_empty:
            print(json.dumps({"ok": True, "empty": True, "export_repo": str(repo)}, ensure_ascii=False))
            return 0
        raise GuardError(f"{MANIFEST_FILE} is missing; run export once before edits")
    drift = _source_drift(manifest)
    if drift and not args.allow_source_drift:
        print("BLOCKED: source files differ from the latest content-redactor export:", file=sys.stderr)
        for item in drift[:80]:
            print(item, file=sys.stderr)
        if len(drift) > 80:
            print(f"... {len(drift) - 80} more", file=sys.stderr)
        return 5
    payload = {
        "ok": True,
        "source_root": str(SOURCE_ROOT),
        "export_repo": str(repo),
        "branch": args.branch,
        "remote": REMOTE_URL,
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo, check=False).stdout.strip(),
        "exported_at": manifest.get("exported_at"),
        "source_drift_count": len(drift),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    repo = _export_repo(args)
    files = _scope_files()
    _ensure_repo(repo, args.branch)
    _ensure_not_behind(repo, args.branch)
    manifest = _manifest_payload(files)
    _copy_scope_into_repo(repo, files, manifest)
    _run(["git", "add", "-A"], cwd=repo)
    dirty = _porcelain(repo)
    if not dirty:
        head = _run(["git", "rev-parse", "HEAD"], cwd=repo, check=False).stdout.strip()
        print(json.dumps({"ok": True, "changed": False, "head": head, "export_repo": str(repo)}, ensure_ascii=False))
        return 0
    message = args.commit_message or f"sync content redactor {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    _run(["git", "commit", "-m", message], cwd=repo)
    if args.push:
        _run(["git", "push", "-u", "origin", args.branch], cwd=repo)
    payload = {
        "ok": True,
        "changed": True,
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
        "export_repo": str(repo),
        "remote": REMOTE_URL,
        "branch": args.branch,
        "files_count": len(files),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = _export_repo(args)
    _ensure_repo(repo, args.branch)
    manifest = _read_manifest(repo)
    payload = {
        "source_root": str(SOURCE_ROOT),
        "export_repo": str(repo),
        "remote": REMOTE_URL,
        "branch": args.branch,
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo, check=False).stdout.strip(),
        "dirty": _porcelain(repo),
        "manifest_exported_at": (manifest or {}).get("exported_at"),
        "source_drift": _source_drift(manifest) if manifest else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-repo", default=None, help=f"Local export repo path (default: {DEFAULT_EXPORT_REPO})")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help=f"Export branch (default: {DEFAULT_BRANCH})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scope = sub.add_parser("scope", help="Print the content/accounts file scope")
    scope.set_defaults(func=cmd_scope)

    preflight = sub.add_parser("preflight", help="Check remote freshness and source/export hash parity")
    preflight.add_argument("--allow-empty", action="store_true", help="Allow first run before initial export")
    preflight.add_argument("--allow-source-drift", action="store_true", help="Report but do not block on source drift")
    preflight.set_defaults(func=cmd_preflight)

    export = sub.add_parser("export", help="Copy scoped files into export repo, commit and push")
    export.add_argument("--commit-message", default=None, help="Git commit message for the export repo")
    export.add_argument("--no-push", dest="push", action="store_false", help="Commit locally without pushing")
    export.set_defaults(func=cmd_export, push=True)

    status = sub.add_parser("status", help="Show export repo and source drift status")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

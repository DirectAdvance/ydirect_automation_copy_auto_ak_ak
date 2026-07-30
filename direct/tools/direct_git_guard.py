#!/usr/bin/env python3
"""Git/deploy guard for Direct automation tasks.

The Direct project is a nested git repository on the Mac and must also be a
nested repository on LXC101. Agent Board tasks use this helper before edits and
after deploy so the running files can be tied back to an exact commit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import direct_git_zones  # noqa: E402


SOURCE_ROOT = Path(__file__).resolve().parents[2]  # home/seoadvanced
TOKEN_ENV_NAMES = ("GIT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
DEFAULT_BRANCH = "ydirect_automation_copy_auto_ak_ak"
MARKER_FILE = ".deploy_version.json"


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
        askpass = Path(tempfile.gettempdir()) / "direct_git_guard_askpass.sh"
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
        raise RuntimeError(f"{' '.join(args)} failed: {msg}")
    return proc


def _repo_root(start: Path) -> Path:
    proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=start)
    return Path(proc.stdout.strip()).resolve()


def _branch(repo: Path) -> str:
    return _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()


def _target_branch(args_branch: str | None) -> str:
    return (args_branch or os.environ.get("DIRECT_GIT_BRANCH") or DEFAULT_BRANCH).strip()


def _porcelain(repo: Path) -> list[str]:
    out = _run(["git", "status", "--porcelain"], cwd=repo).stdout
    return [line for line in out.splitlines() if line.strip()]


def _tracked_path(repo: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path.resolve().relative_to(repo))
    return str(path)


def _dirty_target_lines(repo: Path, target_files: list[str]) -> list[str]:
    lines = _porcelain(repo)
    if not target_files:
        return lines
    targets = {_tracked_path(repo, item) for item in target_files}
    dirty: list[str] = []
    for line in lines:
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path in targets:
            dirty.append(line)
    return dirty


def _dirty_scope_lines(lines: list[str], scope: str) -> list[str]:
    if scope == "all":
        return lines
    dirty: list[str] = []
    for line in lines:
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        zone = direct_git_zones.classify(path)
        if zone == scope:
            dirty.append(line)
    return dirty


def _remote_exists(repo: Path, branch: str) -> bool:
    proc = _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=repo, check=False)
    return proc.returncode == 0


def _ahead_behind(repo: Path, branch: str) -> tuple[int, int]:
    proc = _run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"],
        cwd=repo,
    )
    left, right = (proc.stdout.strip().split() + ["0", "0"])[:2]
    return int(left), int(right)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_preflight(args: argparse.Namespace) -> int:
    repo = _repo_root(Path(args.cwd).resolve())
    branch = _target_branch(args.branch)
    current = _branch(repo)
    if current != branch:
        print(f"BLOCKED: current branch is {current!r}, expected {branch!r}", file=sys.stderr)
        return 2

    _run(["git", "fetch", "origin", branch], cwd=repo, check=False)
    if _remote_exists(repo, branch):
        ahead, behind = _ahead_behind(repo, branch)
        if behind:
            # Scope-фильтрованный статус: репо общий на несколько зон/сессий, чужие
            # незакоммиченные файлы вне args.scope не должны ни блокировать авто-heal,
            # ни (тем более) быть кандидатом на снос — see .claude/sdd/ ...report.md.
            scoped_dirty = _dirty_scope_lines(_porcelain(repo), args.scope)
            if not scoped_dirty and ahead == 0:
                # ff-only вместо reset --hard: если входящие коммиты всё же задевают
                # путь, грязный вне scope, git безопасно откажет вместо сноса чужой работы.
                merge = _run(
                    ["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo, check=False,
                )
                if merge.returncode == 0:
                    print(
                        f"AUTO-HEALED: {repo} was {behind} commit(s) behind origin/{branch} — "
                        f"fast-forwarded automatically (clean in-scope tree, no local-only "
                        f"commits, git — источник истины)",
                        file=sys.stderr,
                    )
                else:
                    msg = (merge.stderr or merge.stdout or "").strip()
                    print(
                        f"BLOCKED: local HEAD is behind origin/{branch} by {behind} commit(s); "
                        f"auto fast-forward failed ({msg}); resolve manually before edits",
                        file=sys.stderr,
                    )
                    return 3
            else:
                reason = (
                    "in-scope uncommitted changes present"
                    if scoped_dirty
                    else f"{ahead} local commit(s) not on origin (diverged, needs manual rebase)"
                )
                print(
                    f"BLOCKED: local HEAD is behind origin/{branch} by {behind} commit(s) and "
                    f"cannot auto-heal ({reason}); resolve manually before edits",
                    file=sys.stderr,
                )
                return 3
    elif args.require_remote:
        print(f"BLOCKED: origin/{branch} does not exist", file=sys.stderr)
        return 4

    dirty = _dirty_target_lines(repo, args.target_file or [])
    if not args.target_file:
        dirty = _dirty_scope_lines(dirty, args.scope)
    if dirty and not args.allow_dirty:
        print("BLOCKED: working tree has uncommitted changes:", file=sys.stderr)
        for line in dirty:
            print(line, file=sys.stderr)
        return 5

    payload = {
        "ok": True,
        "repo": str(repo),
        "branch": branch,
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
        "dirty_count": len(dirty),
        "scope": args.scope,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_marker(args: argparse.Namespace) -> int:
    repo = _repo_root(Path(args.cwd).resolve())
    branch = _target_branch(args.branch)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    files: dict[str, str] = {}
    for item in args.file or []:
        rel = _tracked_path(repo, item)
        path = repo / rel
        if path.is_file():
            files[rel] = _sha256(path)
    marker = {
        "project": "seoadvanced/direct",
        "branch": branch,
        "commit": commit,
        "deployed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "files_sha256": files,
        "services": args.service or [],
    }
    out = repo / MARKER_FILE
    out.write_text(json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(marker, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = _repo_root(Path(args.cwd).resolve())
    marker_path = repo / MARKER_FILE
    payload = {
        "repo": str(repo),
        "branch": _branch(repo),
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
        "dirty": _dirty_scope_lines(_porcelain(repo), args.scope),
        "marker": json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".", help="Path inside the Direct git repository")
    parser.add_argument("--branch", default=None, help=f"Expected branch (default: {DEFAULT_BRANCH})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    preflight = sub.add_parser("preflight", help="Check branch, remote freshness and dirty files before edits")
    preflight.add_argument("--target-file", action="append", default=[], help="File that the task plans to edit")
    preflight.add_argument(
        "--scope",
        choices=("all", "copy", "content", "slepki", "automation"),
        default="all",
        help="Dirty-file scope",
    )
    preflight.add_argument("--allow-dirty", action="store_true", help="Allow existing dirty files")
    preflight.add_argument("--require-remote", action="store_true", help="Block when origin/<branch> is absent")
    preflight.set_defaults(func=cmd_preflight)

    marker = sub.add_parser("write-marker", help=f"Write {MARKER_FILE} for the deployed commit")
    marker.add_argument("--file", action="append", default=[], help="File to include in marker sha256 map")
    marker.add_argument("--service", action="append", default=[], help="Restarted/verified service name")
    marker.set_defaults(func=cmd_marker)

    status = sub.add_parser("status", help="Show git state and deploy marker")
    status.add_argument(
        "--scope",
        choices=("all", "copy", "content", "slepki", "automation"),
        default="all",
        help="Dirty-file scope",
    )
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

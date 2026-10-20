"""Agent-side commands bound to ARTEL_TASK: `artel add`, `artel note`, `artel heavy` (spec §6, §8)."""
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import artel
from artel import journal, platform, procs, transitions
from artel.board import UnknownTask, open_board
from artel.config import load_config
from artel.errors import ArtelError, UsageError
from artel.gitutil import Repo


def _task_from_env() -> int | None:
    value = os.environ.get("ARTEL_TASK")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ArtelError(f"ARTEL_TASK is not a task id: {value!r}") from None


def clean_area(raw: str) -> str:
    """One repository-relative path with `/` separators, lexically resolved; "" is the whole repository.
    Different spellings of one path must compare equal in pick.areas_overlap (src\\a vs src/a on Windows)."""
    if not raw.strip():
        raise UsageError("--area must not be empty (omit it for the whole repository)")
    text = raw.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise UsageError(f"--area {raw!r}: must be relative to the repository, not an absolute path")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise UsageError(f"--area {raw!r}: escapes the repository")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def run_add(args) -> int:
    repo = Repo.discover(Path.cwd())
    if not args.title.strip():
        raise UsageError("a task needs a title")
    if args.review is not None and args.review < 0:
        raise UsageError("--review must be at least 0")
    areas = [clean_area(a) for a in args.area]
    board = open_board(repo.board_path)
    try:
        for after_id in args.after:
            try:
                board.get_task(after_id)
            except UnknownTask:
                raise UsageError(f"--after {after_id}: no such task") from None
        parent = _task_from_env()
        event, created_by = ("add_agent", f"agent:{parent}") if parent is not None else ("add_human", "human")
        task_id = transitions.create(
            board, event, title=args.title, created_by=created_by, body=args.body or "", area=areas,
            priority=args.priority, attack=args.attack, strong=args.strong, review_rounds=args.review,
            merge=args.merge, after=args.after)
        if args.review == 0:
            print("warning: --review 0 behaves like 1: one review, blocking findings go to you (no fix round)",
                  file=sys.stderr)
        print(f"#{task_id} {board.get_task(task_id).state}")
    finally:
        board.close()
    return 0


def run_note(args) -> int:
    task_id, stage = _task_from_env(), os.environ.get("ARTEL_STAGE")
    if task_id is None or not stage:
        raise ArtelError("artel note is for agents; ARTEL_TASK is not set")
    round_text = os.environ.get("ARTEL_ROUND")
    repo = Repo.discover(Path.cwd())
    board = open_board(repo.board_path)
    try:
        board.get_task(task_id)   # a stale ARTEL_TASK is an error, not a foreign-key traceback
    except UnknownTask:
        board.close()
        raise ArtelError(f"artel note: ARTEL_TASK {task_id} is not a task on this board") from None
    try:
        journal.append(board, args.kind, args.text, author=f"agent:{stage}", task_id=task_id, stage=stage,
                       round=int(round_text) if round_text and round_text.isdigit() else None,
                       basis=args.basis or "")
    except journal.EntryTooLong as exc:
        print(f"artel: {exc}", file=sys.stderr)
        return 2
    finally:
        board.close()
    return 0


def tool_pattern_matches(patterns: Sequence[str], argv: Sequence[str]) -> bool:
    command = " ".join(argv)
    for pattern in patterns:
        if pattern.startswith("Bash(") and pattern.endswith(")"):
            if fnmatch.fnmatchcase(command, pattern[len("Bash("):-1]):   # no normcase: same on every OS
                return True
    return False


def run_heavy(args) -> int:
    cmd = list(args.cmd)
    if not cmd:
        raise UsageError("artel heavy: a command is required (artel heavy -- CMD)")
    repo = Repo.discover(Path.cwd())
    cfg = load_config(repo)
    if not tool_pattern_matches(cfg["commands.project_tools"], cmd):
        raise UsageError(f"artel heavy: {' '.join(cmd)!r} matches no Bash(...) pattern in commands.project_tools")
    argv = procs.heavy_argv(cmd, cfg["commands.heavy_lock"], cfg["shell"])
    try:
        return subprocess.run(platform.command_line(argv)).returncode
    except OSError as exc:
        raise ArtelError(f"artel heavy: cannot run {argv[0]}: {exc}") from exc


def ensure_shim(repo: Repo) -> Path:
    return platform.write_shim(repo.artel_dir / "bin", sys.executable)


def agent_env(repo: Repo, *, task_id: int, stage: str, round: int, base_env: Mapping[str, str]) -> dict[str, str]:
    env = dict(base_env)

    def key(name: str) -> str:   # Windows environment names are case-insensitive
        return next((k for k in env if k.upper() == name), name)

    bin_dir = ensure_shim(repo)
    env.update(ARTEL_TASK=str(task_id), ARTEL_STAGE=stage, ARTEL_ROUND=str(round), PYTHONIOENCODING="utf-8")
    path_key, pp_key = key("PATH"), key("PYTHONPATH")
    env[path_key] = os.pathsep.join(p for p in (str(bin_dir), env.get(path_key, "")) if p)   # no empty entry
    package_root = str(Path(artel.__file__).resolve().parent.parent)   # the shim works from a checkout too
    env[pp_key] = os.pathsep.join(p for p in (package_root, env.get(pp_key, "")) if p)
    return env

"""Slots: the worktree pool agents work in (spec §3, §3.1 "Leaving a slot", §5 steps 3 and 6).

Every reset, clean, switch and merge in this module runs only after `guard()` accepted the path as a
registered slot under `slots_dir`: a corrupt board row or a hand-made worktree must never be scrubbed.
"""
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from artel import procs
from artel.errors import ArtelError, GitError
from artel.gitutil import INTEGRATION, Repo, task_branch

_NO_BYTES = b"<missing>"


class SlotGuardError(ArtelError):
    """The path is not a registered slot under slots_dir: nothing was run."""


class SlotBusy(ArtelError):
    def __init__(self, where: Path, branch: str | None = None):
        self.where = Path(where)
        what = f"{branch} is" if branch else "the branch is"
        super().__init__(f"held: {what} checked out at {self.where}")


def merge_identity_args(repo: Repo) -> list[str]:
    """[] when git can name a committer by itself, else a throwaway identity for the merge commit (PD-16)."""
    def configured(config_key: str, env_key: str) -> bool:
        if os.environ.get(env_key):
            return True
        result = repo.git("config", config_key, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    if configured("user.email", "GIT_COMMITTER_EMAIL") and configured("user.name", "GIT_COMMITTER_NAME"):
        return []
    return ["-c", "user.name=artel", "-c", "user.email=artel@localhost"]


def _slots_root(repo: Repo, cfg) -> Path:
    return repo.root / cfg["slots_dir"]


def slot_path(repo: Repo, cfg, n: int) -> Path:
    return _slots_root(repo, cfg) / f"artel-{n}"


def _inside(path: Path, parent: Path) -> bool:
    return path != parent and parent in path.parents


def ensure_slots(repo: Repo, board, cfg, count: int) -> list[int]:
    if _inside(_slots_root(repo, cfg).resolve(), repo.common_dir.resolve()):
        raise ArtelError(f"slots_dir {cfg['slots_dir']!r} is inside .git: slots must live in the working tree")
    repo.git("worktree", "prune")
    known = {wt.path.resolve() for wt in repo.worktrees()}
    for n in range(1, count + 1):
        path = slot_path(repo, cfg, n)
        if path.resolve() not in known:
            if path.exists() and any(path.iterdir()):
                raise ArtelError(f"{path} exists but is not a git worktree; remove it or change slots_dir")
            repo.git("worktree", "add", "--detach", str(path), INTEGRATION)
        board.upsert_slot(n, str(path))
    return list(range(1, count + 1))


def checked_out_elsewhere(repo: Repo, branch: str, *, except_path: Path | None = None) -> Path | None:
    mine = None if except_path is None else Path(except_path).resolve()
    for wt in repo.worktrees():
        if wt.branch == branch and (mine is None or wt.path.resolve() != mine):
            return wt.path
    return None


def guard(repo: Repo, board, cfg, n: int) -> Path:
    expected = slot_path(repo, cfg, n)
    slot = next((s for s in board.list_slots() if s.n == n), None)
    if slot is None:
        raise SlotGuardError(f"slot {n} is not registered")
    if Path(slot.path).resolve() != expected.resolve():
        raise SlotGuardError(f"slot {n} is registered at {slot.path}, expected {expected}")
    if not _inside(expected.resolve(), _slots_root(repo, cfg).resolve()):
        raise SlotGuardError(f"{expected} is not inside {_slots_root(repo, cfg)}")
    if not expected.is_dir():
        raise SlotGuardError(f"slot {n} directory {expected} does not exist")
    top = repo.git("rev-parse", "--show-toplevel", cwd=expected, check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != expected.resolve():
        raise SlotGuardError(f"{expected} is not the top level of a git worktree")
    return expected


def scrub(repo: Repo, path: Path, keep: Sequence[str]) -> None:
    """Private: only called after guard()."""
    if repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=path, check=False).returncode == 0:
        repo.git("merge", "--abort", cwd=path, check=False)
    repo.git("reset", "--hard", cwd=path)
    excludes = [arg for pattern in keep for arg in ("-e", pattern)]
    repo.git("clean", "-ffdx", *excludes, cwd=path)


def _head(repo: Repo, path: Path) -> str:
    return repo.git("rev-parse", "HEAD", cwd=path).stdout.strip()


def prepare_slot(repo: Repo, board, cfg, n: int, task_id: int) -> str:
    path = guard(repo, board, cfg, n)
    branch = task_branch(task_id)
    elsewhere = checked_out_elsewhere(repo, branch, except_path=path)
    if elsewhere is not None:
        raise SlotBusy(elsewhere, branch)
    scrub(repo, path, cfg["slot_keep"])
    if repo.branch_exists(branch):
        repo.git("switch", branch, cwd=path)
    else:
        repo.git("switch", "-C", branch, INTEGRATION, cwd=path)
    return _head(repo, path)


def prepare_detached(repo: Repo, board, cfg, n: int) -> str:
    path = guard(repo, board, cfg, n)
    scrub(repo, path, cfg["slot_keep"])
    repo.git("switch", "--detach", INTEGRATION, cwd=path)
    return _head(repo, path)


def start_conflict_merge(repo: Repo, board, cfg, n: int) -> list[str]:
    path = guard(repo, board, cfg, n)
    merged = repo.git(*merge_identity_args(repo), "merge", "--no-commit", "--no-ff", INTEGRATION,
                      cwd=path, check=False)
    if merged.returncode not in (0, 1):
        raise GitError(["merge", "--no-commit", "--no-ff", INTEGRATION], merged.stderr)
    out = repo.git("diff", "--name-only", "--diff-filter=U", cwd=path).stdout
    return [line for line in out.splitlines() if line]


def leave_slot(repo: Repo, board, cfg, n: int) -> None:
    path = guard(repo, board, cfg, n)
    scrub(repo, path, cfg["slot_keep"])
    repo.git("switch", "--detach", cwd=path)


def setup_hash(path: Path, setup_cmd, inputs: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(setup_cmd).encode("utf-8") + b"\0")
    for name in inputs:
        rel = name.replace("\\", "/")
        target = Path(path) / rel
        try:
            data = target.read_bytes() if target.is_file() else _NO_BYTES
        except OSError:
            data = _NO_BYTES
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def setup_changed(path: Path, cfg, stored_hash: str | None) -> bool:
    return setup_hash(path, cfg["commands.setup"], cfg["commands.setup_inputs"]) != stored_hash


def _log_path(repo: Repo, board, task_id: int | None) -> tuple[Path, int]:
    if task_id is None:   # the base job (PD-34): no runs row; n = 1 + existing setup logs
        directory = repo.logs_dir / "base"
        existing = list(directory.glob("setup-*.log")) if directory.is_dir() else []
        n = len(existing) + 1
    else:
        directory = repo.logs_dir / str(task_id)
        n = 1 + sum(1 for r in board.list_runs(task_id) if r.stage == "setup")
    return directory / f"setup-{n}.log", n


def run_setup_if_needed(repo: Repo, board, cfg, n: int, path: Path, *, task_id: int | None,
                        on_start=None, stop_event=None, env: Mapping[str, str] | None = None) -> str:
    setup = cfg["commands.setup"]
    if not setup:
        return "skipped"
    wanted = setup_hash(path, setup, cfg["commands.setup_inputs"])
    slot = next((s for s in board.list_slots() if s.n == n), None)
    if slot is not None and slot.setup_hash == wanted:
        return "skipped"
    log_path, _ = _log_path(repo, board, task_id)
    run = None
    if task_id is not None:
        run = board.start_run(task_id=task_id, stage="setup", round=board.get_task(task_id).round, model=None,
                              model_reason=None, slot=n, log_path=str(log_path))
    outcome, exit_code, timed_out, killed = "failed", None, False, False
    try:
        try:
            argv = procs.heavy_argv(setup, cfg["commands.heavy_lock"], cfg["shell"])
            result = procs.run_tree(
                argv, cwd=path, env=env, timeout=cfg["setup_timeout"], kill_grace=cfg["kill_grace"],
                log_path=log_path, merge_stderr=True, stop_event=stop_event,
                on_start=None if on_start is None else (lambda pid, group, fp: on_start("setup", pid, group, fp)))
        except ArtelError as exc:   # the command could not even start: a failed setup with a visible reason
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"artel: setup could not start: {exc}\n")
        else:
            exit_code, timed_out = result.returncode, result.timed_out
            killed = result.timed_out or result.stopped
            if result.stopped:
                outcome = "stopped"
            elif result.timed_out:
                outcome = "timeout"
            elif result.returncode == 0:
                outcome = "ok"
    finally:
        if run is not None:
            board.finish_run(run.id, outcome=outcome, usage={}, exit_code=exit_code, timed_out=timed_out,
                             killed=killed)
        board.set_slot_setup_hash(n, wanted if outcome == "ok" else None)
    return outcome


def reconcile_slots(repo: Repo, board, cfg) -> None:
    """Prune vanished worktrees and detach every slot that exists; missing slots are re-created by ensure_slots."""
    repo.git("worktree", "prune")
    first_error = None
    for slot in board.list_slots():
        if not Path(slot.path).exists():
            continue
        try:
            leave_slot(repo, board, cfg, slot.n)
        except SlotGuardError as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def task_branch_exists(repo: Repo, task_id: int) -> bool:
    return repo.branch_exists(task_branch(task_id))


def delete_task_branch(repo: Repo, board, cfg, task_id: int) -> None:
    branch = task_branch(task_id)
    if not repo.branch_exists(branch):
        return
    slots = {Path(s.path).resolve(): s.n for s in board.list_slots()}
    for wt in repo.worktrees():
        if wt.branch != branch:
            continue
        n = slots.get(wt.path.resolve())
        if n is None:
            raise SlotBusy(wt.path, branch)
        leave_slot(repo, board, cfg, n)
    repo.git("branch", "-D", branch)


def apply_git_effects(repo: Repo, board, cfg, task_id: int, effects: Sequence[str]) -> None:
    if any(effect in ("delete_branch", "reset_branch") for effect in effects):
        delete_task_branch(repo, board, cfg, task_id)

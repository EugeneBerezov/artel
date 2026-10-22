"""Slots: the worktree pool agents work in (spec §3, §3.1 "Leaving a slot", §5 steps 3 and 6).

Every reset, clean, switch and merge in this module runs only after `guard()` accepted the path as a
registered slot under `slots_dir`: a corrupt board row or a hand-made worktree must never be scrubbed.
"""
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from artel import platform, procs
from artel.errors import ArtelError, GitError
from artel.gitutil import INTEGRATION, Repo, task_branch

_NO_BYTES = b"<missing>"
# an inherited GIT_DIR & co. would redirect every slot command to another repository (e.g. artel run from a hook)
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")


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


def clean_env() -> dict[str, str]:
    """The environment without the variables that would redirect git to another repository."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV}


@contextlib.contextmanager
def no_hooks(repo: Repo):
    """The git options that keep an agent-planted hook or fsmonitor from running in the runner: a hooks directory
    that is new, empty and named at random for this one call (nothing an agent wrote beforehand is in it)."""
    repo.tmp_dir.mkdir(parents=True, exist_ok=True)
    empty = Path(tempfile.mkdtemp(prefix="nohooks-", dir=repo.tmp_dir))
    try:
        yield ["-c", f"core.hooksPath={empty}", "-c", "core.fsmonitor=false", "--no-optional-locks"]
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def git_hardened(repo: Repo, *args: str, cwd: Path | None = None, check: bool = True, input: str | None = None):
    with no_hooks(repo) as options:
        return repo.git(*options, *args, cwd=cwd, check=check, input=input, env=clean_env(),
                        errors="surrogateescape")


def git_hardened_bytes(repo: Repo, *args: str, cwd: Path | None = None) -> bytes:
    with no_hooks(repo) as options:
        return repo.git_bytes(*options, *args, cwd=cwd, env=clean_env())


def _git(repo: Repo, *args: str, cwd: Path, check: bool = True):
    """Run git inside a slot with the repository-redirecting environment variables removed."""
    return repo.git(*args, cwd=cwd, check=check, env=clean_env())


def _slots_root(repo: Repo, cfg) -> Path:
    return repo.root / cfg["slots_dir"]


def slot_path(repo: Repo, cfg, n: int) -> Path:
    return _slots_root(repo, cfg) / f"artel-{n}"


def _inside(path: Path, parent: Path) -> bool:
    return path != parent and parent in path.parents


def _check_slots_dir(repo: Repo, cfg) -> None:
    root = _slots_root(repo, cfg).resolve()
    common = repo.common_dir.resolve()
    if root == common or _inside(root, common) or ".git" in root.parts:
        raise ArtelError(f"slots_dir {cfg['slots_dir']!r} is inside .git: slots must live in the working tree")
    main = repo.root.resolve()
    if root == main or root in main.parents:
        raise ArtelError(f"slots_dir {cfg['slots_dir']!r} must be a directory below the repository root")


def _kept(line_path: str, keep: Sequence[str]) -> bool:
    name = line_path.strip().strip('"').replace("\\", "/")
    return any(fnmatch.fnmatchcase(name, pattern) or name.startswith(pattern.rstrip("/") + "/")
               or name.rstrip("/") == pattern.rstrip("/") for pattern in keep)


def _adoptable(repo: Repo, wt, keep: Sequence[str], registered: bool) -> str | None:
    """Why an existing worktree at a slot path must not be taken over, or None. A slots_dir such as
    `.claude/worktrees` is also where Claude Code and users keep their own worktrees: scrubbing one would
    destroy their work."""
    if wt.branch is not None and not wt.branch.startswith("artel/"):
        return f"it has branch {wt.branch!r} checked out"
    if registered:
        return None   # artel created this slot; leftover dirt from a killed agent is scrubbed on the next use
    status = _git(repo, "status", "--porcelain", cwd=wt.path, check=False)
    dirty = [l for l in status.stdout.splitlines() if not _kept(l[3:].split(" -> ")[-1], keep)]
    if status.returncode != 0 or dirty:
        return "it has uncommitted changes"
    return None


def _exclude_slots_dir(repo: Repo, cfg) -> None:
    """Hide the slots from `git status` in the main worktree: one `/<slots_dir>/` line in info/exclude, once."""
    try:
        rel = Path(os.path.normpath(_slots_root(repo, cfg))).relative_to(repo.root).as_posix()   # `a/../b` is `b`
    except ValueError:
        return   # slots_dir outside the repository: nothing in the main worktree to hide
    if rel == ".." or rel.startswith("../"):
        return
    line = "/" + re.sub(r"([\\*?\[!# ])", r"\\\1", rel) + "/"
    exclude = repo.common_dir / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    if line in (l.rstrip("\r") for l in text.splitlines()):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not text or text.endswith("\n") else "\n"
    with open(exclude, "a", encoding="utf-8", newline="") as f:
        f.write(f"{prefix}{line}\n")


def ensure_slots(repo: Repo, board, cfg, count: int) -> list[int]:
    _check_slots_dir(repo, cfg)
    _exclude_slots_dir(repo, cfg)
    repo.git("worktree", "prune")
    known = {wt.path.resolve(): wt for wt in repo.worktrees()}
    registered = {s.n: Path(s.path).resolve() for s in board.list_slots()}
    for n in range(1, count + 1):
        path = slot_path(repo, cfg, n)
        if path.resolve() == repo.root.resolve():
            raise ArtelError(f"slot {n} would be the main worktree {path}")
        if platform.is_link_or_junction(path):
            raise ArtelError(f"{path} is a link; a slot must be a real directory")
        if path.exists() and not path.is_dir():
            raise ArtelError(f"{path} exists and is not a directory; remove it or change slots_dir")
        if path.resolve() in known:
            why = _adoptable(repo, known[path.resolve()], cfg["slot_keep"], registered.get(n) == path.resolve())
            if why:
                raise ArtelError(f"{path} is an existing git worktree that artel did not create ({why}); "
                                 "move it or change slots_dir")
        if path.resolve() not in known:
            if path.exists() and any(path.iterdir()):
                raise ArtelError(f"{path} exists but is not a git worktree; remove it or change slots_dir")
            repo.git("worktree", "add", "--detach", str(path), INTEGRATION)
            board.upsert_slot(n, str(path))
            board.set_slot_setup_hash(n, None)   # a re-created slot has no installed dependencies
        else:
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
    if platform.is_link_or_junction(expected):
        raise SlotGuardError(f"{expected} is a link: it may lead to somebody else's worktree")
    if Path(slot.path).resolve() != expected.resolve():
        raise SlotGuardError(f"slot {n} is registered at {slot.path}, expected {expected}")
    if not _inside(expected.resolve(), _slots_root(repo, cfg).resolve()):
        raise SlotGuardError(f"{expected} is not inside {_slots_root(repo, cfg)}")
    if expected.resolve() != _slots_root(repo, cfg).resolve() / expected.name:
        raise SlotGuardError(f"{expected} resolves to a different directory than its slot name")
    if not expected.is_dir():
        raise SlotGuardError(f"slot {n} directory {expected} does not exist")
    if expected.resolve() == repo.root.resolve() or expected.resolve() in repo.root.resolve().parents:
        raise SlotGuardError(f"{expected} is the main worktree or contains it")
    info = _git(repo, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--git-dir",
                cwd=expected, check=False)
    lines = info.stdout.splitlines()
    if info.returncode != 0 or len(lines) != 3:
        raise SlotGuardError(f"{expected} is not a git worktree")
    top, common, git_dir = (Path(line).resolve() for line in lines)
    if top != expected.resolve():
        raise SlotGuardError(f"{expected} is not the top level of a git worktree")
    if common != repo.common_dir.resolve():
        raise SlotGuardError(f"{expected} belongs to another repository")
    if git_dir.parent != common / "worktrees":
        raise SlotGuardError(f"{expected} is not a linked worktree of this repository")
    try:   # the admin directory must belong to THIS directory: a copied or re-pointed .git file names another one
        owner = Path((git_dir / "gitdir").read_text(encoding="utf-8").strip())
        if not owner.is_absolute():
            owner = git_dir / owner
    except OSError:
        raise SlotGuardError(f"{expected}: the worktree administration data is unreadable") from None
    if owner.resolve() != (expected / ".git").resolve():
        raise SlotGuardError(f"{expected} uses the worktree data of {owner.parent}")
    return expected


_STATE_PATHS = ("rebase-merge", "rebase-apply", "BISECT_LOG", "CHERRY_PICK_HEAD", "REVERT_HEAD", "MERGE_HEAD",
                "index.lock")


def scrub(repo: Repo, path: Path, keep: Sequence[str]) -> None:
    """Private: only called after guard()."""
    asked = git_hardened(repo, "rev-parse", "--path-format=absolute", *[a for name in _STATE_PATHS
                                                               for a in ("--git-path", name)], cwd=path)
    state = {name: Path(line).exists() for name, line in zip(_STATE_PATHS, asked.stdout.splitlines())}
    if state["index.lock"]:   # a writer killed during `git add`/`git commit` leaves it; no process of the slot is alive
        for name, line in zip(_STATE_PATHS, asked.stdout.splitlines()):
            if name == "index.lock":
                try:
                    Path(line).unlink()
                except OSError:
                    pass
    if state["rebase-merge"] or state["rebase-apply"]:   # a writer killed mid-rebase or mid-am wedges the slot
        git_hardened(repo, "rebase", "--abort", cwd=path, check=False)
        git_hardened(repo, "am", "--abort", cwd=path, check=False)
        if any(Path(line).exists() for name, line in zip(_STATE_PATHS, asked.stdout.splitlines())
               if name.startswith("rebase")):
            git_hardened(repo, "rebase", "--quit", cwd=path, check=False)
    if state["BISECT_LOG"]:
        git_hardened(repo, "bisect", "reset", cwd=path, check=False)
    if state["CHERRY_PICK_HEAD"]:
        git_hardened(repo, "cherry-pick", "--quit", cwd=path, check=False)
    if state["REVERT_HEAD"]:
        git_hardened(repo, "revert", "--quit", cwd=path, check=False)
    if state["MERGE_HEAD"]:
        git_hardened(repo, "merge", "--abort", cwd=path, check=False)
    git_hardened(repo, "reset", "--hard", cwd=path)
    clear_index_flags(repo, path)   # after the reset: an unmerged entry is gone by then
    git_hardened(repo, "reset", "--hard", cwd=path)   # a file the flags hid is now compared with HEAD (PD-66 (aa))
    excludes = [arg for pattern in keep for arg in ("-e", pattern)]
    git_hardened(repo, "clean", "-ffdx", *excludes, cwd=path)


def _head(repo: Repo, path: Path) -> str:
    return _git(repo, "rev-parse", "HEAD", cwd=path).stdout.strip()


def prepare_slot(repo: Repo, board, cfg, n: int, task_id: int) -> str:
    path = guard(repo, board, cfg, n)
    branch = task_branch(task_id)
    repo.git("worktree", "prune")   # a vanished slot must not look like a holder of the branch
    elsewhere = checked_out_elsewhere(repo, branch, except_path=path)
    if elsewhere is not None:
        raise SlotBusy(elsewhere, branch)
    scrub(repo, path, cfg["slot_keep"])
    if repo.branch_exists(branch):
        git_hardened(repo, "switch", branch, cwd=path)
    else:
        git_hardened(repo, "switch", "-C", branch, INTEGRATION, cwd=path)
    return _head(repo, path)


def prepare_detached(repo: Repo, board, cfg, n: int) -> str:
    path = guard(repo, board, cfg, n)
    scrub(repo, path, cfg["slot_keep"])
    git_hardened(repo, "switch", "--detach", INTEGRATION, cwd=path)
    return _head(repo, path)


def start_conflict_merge(repo: Repo, board, cfg, n: int) -> list[str]:
    path = guard(repo, board, cfg, n)
    merged = _git(repo, *merge_identity_args(repo), "merge", "--no-commit", "--no-ff", INTEGRATION,
                  cwd=path, check=False)
    if merged.returncode not in (0, 1):
        raise GitError(["merge", "--no-commit", "--no-ff", INTEGRATION], merged.stderr)
    out = _git(repo, "diff", "--name-only", "--diff-filter=U", "-z", cwd=path).stdout   # -z: no C-quoting
    return [name for name in out.split("\0") if name]


def _setup_outputs_key(n: int) -> str:
    return f"setup_outputs:{n}"


HIDING_TAGS = "Shs"   # `ls-files -v`: skip-worktree, assume-unchanged (never `M`: unmerged)


def clear_index_flags(repo: Repo, path: Path) -> None:
    """Skip-worktree and assume-unchanged flags hide edits to tracked files. Never raises."""
    listing = git_hardened(repo, "ls-files", "-v", "-z", cwd=path, check=False).stdout
    hidden = [e[2:] for e in listing.split("\0") if e and e[0] in HIDING_TAGS]
    for flag in ("--no-skip-worktree", "--no-assume-unchanged"):   # one flag per call: two lose the first
        if hidden:
            git_hardened(repo, "update-index", flag, "-z", "--stdin", cwd=path, check=False,
                         input="\0".join(hidden) + "\0")


def forget_setup(board, n: int) -> None:
    """The slot's setup output is gone (or was never proven): setup runs again at its next stage."""
    board.set_slot_setup_hash(n, None)
    board.del_meta(_setup_outputs_key(n))


def leave_slot(repo: Repo, board, cfg, n: int) -> None:
    path = guard(repo, board, cfg, n)
    recorded = board.get_meta(_setup_outputs_key(n))
    if recorded is not None:
        try:
            outputs = json.loads(recorded)
        except ValueError:
            outputs = []
        if any(not _kept(name, cfg["slot_keep"]) for name in outputs):   # PD-73: the scrub deletes setup output;
            forget_setup(board, n)                                       # forget first: a crash must not keep the hash
    scrub(repo, path, cfg["slot_keep"])
    git_hardened(repo, "switch", "--detach", cwd=path)


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


def _untracked_tops(repo: Repo, path: Path) -> set[str]:
    """Top-level paths git shows as untracked or ignored: what a setup command can leave behind for a scrub."""
    out = _git(repo, "status", "--porcelain=v1", "--ignored", "--untracked-files=normal", "-z", cwd=path,
               check=False).stdout
    tops, skip = set(), False
    for entry in out.split("\0"):
        if skip:                  # the source path of a rename or copy
            skip = False
        elif entry[:2] in ("??", "!!"):
            tops.add(entry[3:].split("/", 1)[0])
        elif entry[:1] in ("R", "C") or entry[1:2] in ("R", "C"):
            skip = True
    return tops


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
    before = _untracked_tops(repo, path)
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
        if outcome == "ok":   # PD-73: what leaving the slot would delete invalidates the hash
            board.set_meta(_setup_outputs_key(n), json.dumps(sorted(_untracked_tops(repo, path) - before)))
        else:
            board.del_meta(_setup_outputs_key(n))
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
        except ArtelError as exc:   # SlotGuardError or a GitError: keep detaching the other slots
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def task_branch_exists(repo: Repo, task_id: int) -> bool:
    return repo.branch_exists(task_branch(task_id))


def delete_task_branch(repo: Repo, board, cfg, task_id: int) -> None:
    branch = task_branch(task_id)
    repo.git("worktree", "prune")
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
        board.del_meta(f"verified:{task_id}")   # PD-45: the verified commit is gone with its branch

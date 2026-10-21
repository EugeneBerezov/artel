"""Repository check around an agent stage (spec §4 "Repository check", PD-66).

A probe, a test or any command an agent runs can reach the shared `.git`: move a ref of another task, add a
stash or a replace ref, change the config, a hook or `info/attributes`, or write into a slot that is idle. `take`
records what a stage must not change, `compare` lists what differs, `restore` puts it back.
The change texts are the interface between `compare` and `restore`: `ref <name> moved|deleted|created`,
`file <name> changed|deleted|created`, `idle slot <n> changed`, `slot HEAD moved`, `worktrees changed`.
"""
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from artel import slots
from artel.errors import GitError
from artel.gitutil import INTEGRATION, Repo

WATCHED_REFS = ("refs/heads/artel/", "refs/stash", "refs/replace/")
_COMMON_DIRS = ("info", "hooks")
_REF = re.compile(r"^ref (\S+) (moved|deleted|created)$")
_FILE = re.compile(r"^file (.+) (changed|deleted|created)$")
_IDLE = re.compile(r"^idle slot (\d+) changed$")


@dataclass(frozen=True)
class Snapshot:
    refs: dict[str, str]
    files: dict[str, bytes]
    worktrees: tuple[str, ...]
    idle_status: dict[int, str]
    head: str
    idle_paths: dict[int, Path] = field(default_factory=dict)   # where each idle slot lives (for `restore`)


def _refs(repo: Repo) -> dict[str, str]:
    out = repo.git("for-each-ref", "--format=%(refname) %(objectname)", *WATCHED_REFS).stdout
    refs = {}
    for line in out.splitlines():
        name, _, sha = line.partition(" ")
        if name and sha:
            refs[name] = sha
    return refs


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _files(repo: Repo, slot_path: Path) -> dict[str, bytes]:
    common = repo.common_dir
    paths = [common / "config"]
    for name in _COMMON_DIRS:
        base = common / name
        if base.is_dir():
            paths.extend(sorted(p for p in base.rglob("*") if p.is_file()))
    asked = slots._git(repo, "rev-parse", "--path-format=absolute", "--git-path", "config.worktree",
                       cwd=slot_path, check=False)
    if asked.returncode == 0 and asked.stdout.strip():
        paths.append(Path(asked.stdout.strip()))
    files = {}
    for path in paths:
        data = _read(path)
        if data is None:
            continue
        try:
            key = path.relative_to(common).as_posix()
        except ValueError:
            key = path.as_posix()
        if key != "info/refs":   # `git gc` / `repack` rewrite it (update-server-info): git's own, runs no code
            files[key] = data
    return files


def _worktrees(repo: Repo) -> tuple[str, ...]:
    out = repo.git("worktree", "list", "--porcelain", "-z", errors="surrogateescape").stdout
    return tuple(token[len("worktree "):] for token in out.split("\0") if token.startswith("worktree "))


def _status(repo: Repo, path: Path) -> str:
    return slots._git(repo, "status", "--porcelain=v1", "-z", cwd=path, check=False).stdout


def take(repo: Repo, slot_path: Path, *, idle_slots: Sequence[tuple[int, Path]] = ()) -> Snapshot:
    return Snapshot(refs=_refs(repo), files=_files(repo, slot_path), worktrees=_worktrees(repo),
                    idle_status={n: _status(repo, p) for n, p in idle_slots},
                    head=slots._git(repo, "rev-parse", "HEAD", cwd=slot_path, check=False).stdout.strip(),
                    idle_paths={n: Path(p) for n, p in idle_slots})


_BRANCH_SECTION = re.compile(rb'^\[branch "artel/[^\n]*\n(?:[^\[\n][^\n]*\n|\n)*', re.MULTILINE)


def _plain_config(data: bytes) -> bytes:
    """The config without `[branch "artel/..."]` sections: switching to, creating or deleting a task branch (the
    runner's own actions in another slot) writes them."""
    text = data if data.endswith(b"\n") else data + b"\n"
    return _BRANCH_SECTION.sub(b"", text)


def compare(before: Snapshot, after: Snapshot, *, own_branch: str | None, may_commit: bool,
            skip_refs: Collection[str], runner_changed_worktrees: bool = False,
            repo: Repo | None = None) -> list[str]:
    """What the stage changed, one short text per change. `refs/heads/artel/integration` and `skip_refs` are the
    runner's. The own branch and the slot HEAD may move only for a writer adding commits (`repo` is needed to
    see that the new tip descends from the old one; without it any move counts)."""
    ignored = {f"refs/heads/{INTEGRATION}", *skip_refs}
    own = f"refs/heads/{own_branch}" if own_branch else None

    def adds_commits(old: str, new: str) -> bool:
        return may_commit and repo is not None and repo.is_ancestor(old, new)

    changes = []
    own_moved = False
    for name in sorted(before.refs.keys() | after.refs.keys()):
        if name in ignored:
            continue
        old, new = before.refs.get(name), after.refs.get(name)
        if old == new:
            continue
        if old is None:
            changes.append(f"ref {name} created")
        elif new is None:
            changes.append(f"ref {name} deleted")
        elif name == own and adds_commits(old, new):
            continue
        else:
            changes.append(f"ref {name} moved")
        own_moved = own_moved or name == own
    for name in sorted(before.files.keys() | after.files.keys()):
        old, new = before.files.get(name), after.files.get(name)
        if name == "config" and old is not None and new is not None:
            old, new = _plain_config(old), _plain_config(new)
        if old != new:
            changes.append(f"file {name} " + ("created" if old is None else "deleted" if new is None else "changed"))
    if before.worktrees != after.worktrees and not runner_changed_worktrees:
        changes.append("worktrees changed")
    for n in sorted(before.idle_status.keys() & after.idle_status.keys()):
        if before.idle_status[n] != after.idle_status[n]:
            changes.append(f"idle slot {n} changed")
    if (before.head and after.head != before.head and not own_moved
            and not (after.head and adds_commits(before.head, after.head))):
        changes.append("slot HEAD moved")
    return changes


def _restore_ref(repo: Repo, name: str, how: str, before: Snapshot, after: Snapshot) -> bool:
    if how == "created":
        result = repo.git("update-ref", "-d", name, after.refs[name], check=False)
    else:   # moved or deleted: back to the old value (an empty old value: the ref must not exist)
        result = repo.git("update-ref", name, before.refs[name], after.refs.get(name, ""), check=False)
    return result.returncode == 0


def _restore_file(repo: Repo, name: str, how: str, before: Snapshot) -> bool:
    path = repo.common_dir / name
    try:
        if how == "created":
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before.files[name])
    except OSError:
        return False
    return True


def _clean_idle(repo: Repo, path: Path) -> bool:
    # not `leave_slot`: that needs the board and the settings, and the slot only has to be clean again
    slots._git(repo, "reset", "--hard", cwd=path, check=False)
    slots._git(repo, "clean", "-ffd", cwd=path, check=False)
    return _status(repo, path) == ""


def restore(repo: Repo, before: Snapshot, after: Snapshot, changes: Sequence[str]) -> list[str]:
    """Put back what `changes` (from `compare`) lists; returns the changes that could not be restored. The slot
    HEAD is not repaired here (leaving the slot detaches it); a changed worktree list is reported only."""
    left = []
    for change in changes:
        try:
            if (m := _REF.match(change)):
                ok = _restore_ref(repo, m.group(1), m.group(2), before, after)
            elif (m := _FILE.match(change)):
                ok = _restore_file(repo, m.group(1), m.group(2), before)
            elif (m := _IDLE.match(change)):
                path = after.idle_paths.get(int(m.group(1)))
                ok = path is not None and _clean_idle(repo, path)
            else:
                ok = change == "slot HEAD moved"
        except (GitError, OSError):
            ok = False
        if not ok:
            left.append(change)
    return left

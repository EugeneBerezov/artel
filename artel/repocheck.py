"""Repository check around an agent stage (spec §4 "Repository check", PD-66).

A probe, a test or any command an agent runs can reach the shared `.git`: move a ref of another task, add a
stash or a replace ref, change the config, a hook or `info/attributes`, or write into a slot that is idle. `take`
records what a stage must not change, `compare` lists what differs, `restore` puts it back.
The change texts are the interface between `compare` and `restore`: `ref <name> moved|deleted|created`,
`file <name> changed|deleted|created`, `idle slot <n> changed`, `slot HEAD moved`, `worktrees changed`.

Nothing an agent planted may run in here: every git call goes through `_g`/`_gb` (no hooks, no fsmonitor, no
optional locks, no inherited GIT_DIR & co.), and `restore` rewrites files before it touches refs.
"""
import os
import re
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from artel import platform, slots
from artel.errors import GitError
from artel.gitutil import INTEGRATION, Repo

WATCHED_REFS = ("refs/heads/artel/", "refs/stash", "refs/replace/")
STASH = "refs/stash"
_COMMON_DIRS = ("info", "hooks")
_RUNNER_PREFIX = "branch.artel/"   # git writes `.remote` and `.merge` when `switch -C` makes a tracking branch
_RUNNER_SUFFIXES = (".remote", ".merge")   # nothing else: `mergeOptions = -s ours` would change the runner's merges
_REF = re.compile(r"^ref (\S+) (moved|deleted|created)(?: \(.*\))?$")
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
    symrefs: dict[str, str] = field(default_factory=dict)       # symbolic refs under the watched prefixes -> target
    modes: dict[str, int] = field(default_factory=dict)         # permission bits of the hook files
    config: dict[str, tuple[str, ...]] = field(default_factory=dict)         # parsed entries, without the runner's
    runner_config: dict[str, tuple[str, ...]] = field(default_factory=dict)  # the `branch.artel/*` entries


def _g(repo: Repo, *args: str, cwd: Path | None = None, check: bool = True, input: str | None = None):
    return slots.git_hardened(repo, *args, cwd=cwd, check=check, input=input)


def _gb(repo: Repo, *args: str, cwd: Path | None = None) -> bytes:
    return slots.git_hardened_bytes(repo, *args, cwd=cwd)


def _exempt(entry: str) -> bool:
    key = entry.partition("\n")[0].lower()
    return key.startswith(_RUNNER_PREFIX) and key.endswith(_RUNNER_SUFFIXES)


def _refs(repo: Repo) -> tuple[dict[str, str], dict[str, str]]:
    out = _g(repo, "for-each-ref", "--format=%(refname)\t%(objectname)\t%(symref)", *WATCHED_REFS).stdout
    refs, symrefs = {}, {}
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        sha, _, target = rest.partition("\t")
        if name:
            refs[name] = sha
            if target:
                symrefs[name] = target
    common = repo.common_dir
    for base in ("refs/heads/artel", "refs/replace"):   # a dangling symbolic ref is not listed by for-each-ref
        directory = common / base
        for path in (sorted(directory.rglob("*")) if directory.is_dir() else ()):
            name = path.relative_to(common).as_posix()
            if name in refs or path.suffix == ".lock" or not path.is_file():
                continue
            try:
                head = path.read_bytes()[:512]
            except OSError:
                continue
            if head.startswith(b"ref:"):
                refs[name] = ""
                symrefs[name] = head[4:].decode("utf-8", "replace").strip()
    return refs, symrefs


def _config_entries(repo: Repo, path: Path, data: bytes) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(entries, runner entries): `git config -f <file> -z --list --no-includes`, parsed by git itself."""
    result = _g(repo, "config", "-f", str(path), "-z", "--list", "--no-includes", check=False)
    if result.returncode != 0:
        return (f"unparseable\n{data!r}",), ()
    entries = [e for e in result.stdout.split("\0") if e]
    runner = tuple(e for e in entries if _exempt(e))
    return tuple(e for e in entries if not _exempt(e)), runner


def _watched_paths(common: Path) -> list[Path]:
    paths = [common / "config", common / "config.worktree", common / "config.lock", common / "config.worktree.lock"]
    paths.extend(sorted(common.glob("worktrees/*/config.worktree")))
    paths.extend(sorted(common.glob("worktrees/*/config.worktree.lock")))
    for base in ("refs/heads/artel", "refs/replace"):   # a stale `<ref>.lock` makes git refuse that ref
        if (common / base).is_dir():
            paths.extend(sorted((common / base).rglob("*.lock")))
    for name in _COMMON_DIRS:
        base = common / name
        if base.is_dir():
            paths.extend(sorted(p for p in base.rglob("*") if p.is_file()))
    return paths


def _is_lock(key: str) -> bool:
    return key.endswith(".lock")


def _is_config(key: str) -> bool:
    return key == "config" or key.endswith("config.worktree")


def _files(repo: Repo):
    common = repo.common_dir
    files, modes, config, runner = {}, {}, {}, {}
    for path in _watched_paths(common):
        key = path.relative_to(common).as_posix()
        if key == "info/refs":   # `git gc` / `repack` rewrite it (update-server-info): git's own, runs no code
            continue
        if _is_lock(key):   # only its presence matters, and git may be writing it right now
            if path.exists():
                files[key] = b""
            continue
        try:
            data = path.read_bytes()
            mode = platform.file_mode(path)
        except OSError:
            continue
        files[key] = data
        if mode is not None:
            modes[key] = mode
        if _is_config(key):
            config[key], runner[key] = _config_entries(repo, path, data)
    return files, modes, config, runner


def _worktrees(repo: Repo) -> tuple[str, ...]:
    out = _g(repo, "worktree", "list", "--porcelain", "-z").stdout
    return tuple(token[len("worktree "):] for token in out.split("\0") if token.startswith("worktree "))


def _porcelain(repo: Repo, path: Path) -> str:
    return _g(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=path, check=False).stdout


def _status(repo: Repo, path: Path) -> str:
    head = _g(repo, "rev-parse", "HEAD", cwd=path, check=False).stdout.strip()
    branch = _g(repo, "symbolic-ref", "-q", "HEAD", cwd=path, check=False).stdout.strip()
    return f"{_porcelain(repo, path)}\0HEAD {head} {branch}"


def take(repo: Repo, slot_path: Path, *, idle_slots: Sequence[tuple[int, Path]] = ()) -> Snapshot:
    refs, symrefs = _refs(repo)
    files, modes, config, runner = _files(repo)
    return Snapshot(refs=refs, files=files, worktrees=_worktrees(repo),
                    idle_status={n: _status(repo, p) for n, p in idle_slots},
                    head=_g(repo, "rev-parse", "HEAD", cwd=slot_path, check=False).stdout.strip(),
                    idle_paths={n: Path(p) for n, p in idle_slots}, symrefs=symrefs, modes=modes, config=config,
                    runner_config=runner)


def compare(before: Snapshot, after: Snapshot, *, own_branch: str | None, may_commit: bool,
            skip_refs: Collection[str], runner_changed_worktrees: bool = False,
            repo: Repo | None = None, skip_files: Collection[str] = ()) -> list[str]:
    """What the stage changed, one short text per change. `refs/heads/artel/integration` and `skip_refs` are the
    runner's. The own branch and the slot HEAD may move only for a writer adding commits (`repo` is needed to
    see that the new tip descends from the old one; without it any move counts). A symbolic ref under a watched
    prefix is always a change. `skip_files` are files another restore of the runner rewrote meanwhile."""
    ignored = {f"refs/heads/{INTEGRATION}", *skip_refs}
    own = f"refs/heads/{own_branch}" if own_branch else None

    def adds_commits(old: str, new: str) -> bool:
        return may_commit and repo is not None and bool(old) and bool(new) and repo.is_ancestor(old, new)

    changes = []
    own_moved = False
    for name in sorted(before.refs.keys() | after.refs.keys()):
        if name in ignored:
            continue
        old, new = before.refs.get(name), after.refs.get(name)
        if old == new and before.symrefs.get(name) == after.symrefs.get(name):
            continue
        if old is None:
            changes.append(f"ref {name} created")
        elif new is None:
            changes.append(f"ref {name} deleted" + (f" (was {old[:12]})" if name == STASH else ""))
        elif (name == own and name not in before.symrefs and name not in after.symrefs
              and adds_commits(old, new)):
            continue
        else:
            changes.append(f"ref {name} moved")
        own_moved = own_moved or name == own
    for name in sorted(before.files.keys() | after.files.keys()):
        old, new = before.files.get(name), after.files.get(name)
        if name in skip_files or (_is_lock(name) and old is not None):   # a lock already there is someone else's
            continue
        if old is not None and new is not None and _is_config(name):
            same = before.config.get(name) == after.config.get(name)
        else:
            same = old == new
        if (same and old is not None and new is not None and name.startswith("hooks/")
                and before.modes.get(name) != after.modes.get(name)):
            same = False   # `chmod -x` on a hook
        if not same:
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


# the tracked-file state of a slot (review stages) ---------------------------------------------------------------

def is_own(change: str, own_branch: str | None) -> bool:
    """A change only the stage itself can make: its own branch or its slot's HEAD."""
    if change == "slot HEAD moved":
        return True
    m = _REF.match(change)
    return bool(own_branch and m and m.group(1) == f"refs/heads/{own_branch}")


def config_left(left: Sequence[str]) -> list[str]:
    """What stayed unrestored that is a git configuration file: the runner pauses (`repo_changed`)."""
    return [c for c in left if (m := _FILE.match(c)) and _is_config(m.group(1))]


def tracked_state(repo: Repo, path: Path) -> tuple[bytes, ...]:
    """What differs from HEAD in the working tree and in the index, and every index flag that hides a tracked file
    (skip-worktree, assume-unchanged)."""
    diffs = tuple(_gb(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", *args, cwd=path)
                  for args in (("HEAD",), ("--cached",)))
    flags = [e for e in _gb(repo, "ls-files", "-v", "-z", cwd=path).split(b"\0") if e[:1] and e[:1].decode("latin-1") in slots.HIDING_TAGS]
    return diffs + (b"\0".join(sorted(flags)),)


def restore_tracked(repo: Repo, path: Path, before: tuple[bytes, ...]) -> bool:
    """Put a slot's tracked files back when they differ from `before`. True when there was a difference."""
    dirty = tracked_state(repo, path) != before
    if dirty:
        slots.clear_index_flags(repo, path)
        _g(repo, "reset", "-q", cwd=path, check=False)
        _g(repo, "checkout", "--", ".", cwd=path, check=False)
    return dirty


# restore ---------------------------------------------------------------------------------------------------------

def _quarantine(quarantine: Path | None, name: str, source: Path) -> None:
    """Copy what is on disk now to the quarantine directory. A failure is not an obstacle to the restore."""
    if quarantine is None:
        return
    try:
        data = source.read_bytes()
        target = quarantine / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError:
        pass


def _replace_file(path: Path, data: bytes, mode: int | None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")   # a name nobody can plan
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        platform.set_file_mode(Path(tmp), mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _restore_config(repo: Repo, path: Path, name: str, before: Snapshot, after: Snapshot) -> bool:
    """Git's own protocol: write `<file>.lock` (O_EXCL: someone else's lock means skip) and rename it over the
    file. The runner's tracking entries are what they are at the end of the stage, removals included."""
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    done = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(before.files[name])
        platform.set_file_mode(lock, before.modes.get(name))
        was, now = before.runner_config.get(name, ()), after.runner_config.get(name, ())
        keys_now = {e.partition("\n")[0] for e in now}
        for key in sorted({e.partition("\n")[0] for e in was} - keys_now):
            _g(repo, "config", "-f", str(lock), "--unset-all", key, check=False)
        for entry in now:
            if entry not in was:
                key, _, value = entry.partition("\n")
                _g(repo, "config", "-f", str(lock), "--add", key, value, check=False)
        os.replace(lock, path)
        done = True
    finally:
        if not done:
            lock.unlink(missing_ok=True)
    return True


def _restore_file(repo: Repo, name: str, how: str, before: Snapshot, after: Snapshot,
                  quarantine: Path | None) -> bool:
    path = repo.common_dir / name
    if _is_lock(name):   # a lock that was not there before: stale, no live git of the runner owns it
        path.unlink(missing_ok=True)
        return True
    if path.is_symlink():
        path = path.resolve()
    try:
        if how == "created":
            _quarantine(quarantine, name, path)
            path.unlink(missing_ok=True)
            return True
        if how == "changed":
            _quarantine(quarantine, name, path)
            if after.files.get(name) == before.files[name]:
                platform.set_file_mode(path, before.modes.get(name))   # a mode-only change
                return True
        if _is_config(name):
            return _restore_config(repo, path, name, before, after)
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(path, before.files[name], before.modes.get(name))
    except OSError:
        return False
    return True


def _restore_ref(repo: Repo, name: str, how: str, before: Snapshot, after: Snapshot) -> bool:
    if name == STASH:
        return False   # never set back: the human's stash lives here too (owner decision); reported only
    symref = name in after.symrefs   # no old-value guard on a symbolic ref: it would name the target's value
    if how == "created":
        return _g(repo, "update-ref", "-d", "--no-deref", name, *([] if symref else [after.refs[name]]),
                  check=False).returncode == 0
    if name in before.symrefs:
        return _g(repo, "symbolic-ref", name, before.symrefs[name], check=False).returncode == 0
    guard = [] if symref else [after.refs.get(name, "")]   # "" = the ref must not exist (it was deleted)
    return _g(repo, "update-ref", "--no-deref", name, before.refs[name], *guard, check=False).returncode == 0


def _clean_idle(repo: Repo, path: Path, keep: Sequence[str], before_status: str) -> bool:
    try:
        slots.scrub(repo, path, keep)
    except GitError:
        return False
    _g(repo, "switch", "--detach", cwd=path, check=False)
    now = _porcelain(repo, path)   # a kept, untracked directory stays in the status, as it did before
    return now in ("", before_status.split("\0HEAD ")[0])


def _order(change: str) -> int:
    m = _FILE.match(change)
    if m:
        return 0 if _is_lock(m.group(1)) else 1   # a stale lock goes first: it blocks the rewrite behind it
    return 2 if _REF.match(change) else 3


def restore(repo: Repo, before: Snapshot, after: Snapshot, changes: Sequence[str], *,
            quarantine: Path | None = None, slot_keep: Sequence[str] = ()) -> list[str]:
    """Put back what `changes` (from `compare`) lists: stale locks, files, refs, then idle slots; returns the
    changes that could not be restored. `refs/stash` is never set back. A file's changed version is copied to
    `quarantine` before it is rewritten. The slot HEAD is not repaired here (leaving the slot detaches it); a
    changed worktree list is reported only."""
    left = []
    for change in sorted(changes, key=_order):   # stable
        try:
            if (m := _FILE.match(change)):
                ok = _restore_file(repo, m.group(1), m.group(2), before, after, quarantine)
            elif (m := _REF.match(change)):
                ok = _restore_ref(repo, m.group(1), m.group(2), before, after)
            elif (m := _IDLE.match(change)):
                n = int(m.group(1))
                path = after.idle_paths.get(n)
                ok = path is not None and _clean_idle(repo, path, slot_keep, before.idle_status.get(n, ""))
            else:
                ok = change == "slot HEAD moved"
        except (GitError, OSError):
            ok = False
        if not ok:
            left.append(change)
    return left

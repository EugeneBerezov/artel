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
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Sequence
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
CHECK_LOCK = threading.RLock()   # take(after), compare and restore of all stages, one at a time (PD-66)
LOCK_GRACE = 2.0                 # a new `*.lock` is stale only if it is still there this many seconds later
_LINK = b"\0symlink\0"            # in `files`: a link under hooks/ or info/, compared and restored as a link
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
    links: dict[str, str] = field(default_factory=dict)         # symbolic links among the watched files -> target
    admin_dirs: frozenset = frozenset()                         # names under <common>/worktrees/ (live worktrees)


@dataclass(frozen=True)
class Restored:
    """One item a restore put back: `removed` is what it took away, `written` what it wrote (PD-66 (x))."""
    kind: str      # "ref" | "file"
    name: str
    removed: object
    written: object


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
        if base.is_symlink():   # a linked directory is one entry, a link: never walked into (its files are not ours)
            paths.append(base)
        elif base.is_dir():
            paths.extend(sorted(p for p in base.rglob("*") if p.is_file() or p.is_symlink()))
    return paths


def _is_lock(key: str) -> bool:
    return key.endswith(".lock")


def _is_config(key: str) -> bool:
    return key == "config" or key.endswith("config.worktree")


def _files(repo: Repo):
    common = repo.common_dir
    files, modes, config, runner, links = {}, {}, {}, {}, {}
    for path in _watched_paths(common):
        key = path.relative_to(common).as_posix()
        target = platform.read_link(path)
        if target is not None:
            links[key] = target
            if not _is_config(key):   # a link under hooks/ or info/ is a link, not its target's content
                files[key] = _LINK + target.encode("utf-8", "surrogateescape")
                continue
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
    return files, modes, config, runner, links


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
    with CHECK_LOCK:
        return _take(repo, slot_path, idle_slots)


def _take(repo: Repo, slot_path: Path, idle_slots: Sequence[tuple[int, Path]]) -> Snapshot:
    refs, symrefs = _refs(repo)
    files, modes, config, runner, links = _files(repo)
    admin = repo.common_dir / "worktrees"
    return Snapshot(refs=refs, files=files, worktrees=_worktrees(repo),
                    idle_status={n: _status(repo, p) for n, p in idle_slots},
                    head=_g(repo, "rev-parse", "HEAD", cwd=slot_path, check=False).stdout.strip(),
                    idle_paths={n: Path(p) for n, p in idle_slots}, symrefs=symrefs, modes=modes, config=config,
                    runner_config=runner, links=links,
                    admin_dirs=frozenset(p.name for p in admin.iterdir()) if admin.is_dir() else frozenset())


def compare(before: Snapshot, after: Snapshot, *, own_branch: str | None, may_commit: bool,
            skip_refs: Collection[str], runner_changed_worktrees: bool = False,
            repo: Repo | None = None, restored: Collection[Restored] = ()) -> list[str]:
    with CHECK_LOCK:
        return _compare(before, after, own_branch, may_commit, skip_refs, runner_changed_worktrees, repo, restored)


def _compare(before: Snapshot, after: Snapshot, own_branch: str | None, may_commit: bool,
             skip_refs: Collection[str], runner_changed_worktrees: bool, repo: Repo | None,
             restored: Collection[Restored]) -> list[str]:
    """What the stage changed, one short text per change. `refs/heads/artel/integration` and `skip_refs` are the
    runner's. The own branch and the slot HEAD may move only for a writer adding commits (`repo` is needed to
    see that the new tip descends from the old one; without it any move counts). A symbolic ref under a watched
    prefix is always a change. `restored`: what another stage's restore put back meanwhile — an item is left out
    only when this stage saw exactly the value that restore removed before and the one it wrote after."""
    ignored = {f"refs/heads/{INTEGRATION}", *skip_refs}
    own = f"refs/heads/{own_branch}" if own_branch else None

    def adds_commits(old: str, new: str) -> bool:
        return may_commit and repo is not None and bool(old) and bool(new) and repo.is_ancestor(old, new)

    skipped = {(r.kind, r.name, _frozen(r.removed), _frozen(r.written)) for r in restored}

    def put_back(kind: str, name: str, old, new) -> bool:
        return (kind, name, _frozen(old), _frozen(new)) in skipped

    changes = []
    own_moved = False
    for name in sorted(before.refs.keys() | after.refs.keys()):
        if name in ignored:
            continue
        old, new = before.refs.get(name), after.refs.get(name)
        if old == new and before.symrefs.get(name) == after.symrefs.get(name):
            continue
        if put_back("ref", name, _ref_value(before, name), _ref_value(after, name)):
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
        if _is_lock(name):
            ref = name[:-len(".lock")]   # the lock of a ref the runner or this stage's own git works on is live
            if old is not None or ref in ignored or ref == own:   # a lock already there is someone else's
                continue
        elif name.startswith("worktrees/") and new is None and name.split("/")[1] not in after.admin_dirs:
            continue   # `git worktree remove` took its admin directory (and the config.worktree in it) away
        elif put_back("file", name, _file_value(before, name), _file_value(after, name)):
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

def _frozen(value):
    return tuple(value) if isinstance(value, list) else value


def _ref_value(snapshot: Snapshot, name: str):
    return (snapshot.refs.get(name), snapshot.symrefs.get(name))


def _file_value(snapshot: Snapshot, name: str):
    if _is_config(name):
        return snapshot.config.get(name)
    return (snapshot.files.get(name), snapshot.modes.get(name))   # a chmod is a change too


def restored_items(before: Snapshot, after: Snapshot, changes: Sequence[str]) -> list[Restored]:
    """What a restore of `changes` took away (`after`'s value) and wrote (`before`'s): the runner's record."""
    items = []
    for change in changes:
        if (m := _REF.match(change)):
            items.append(Restored("ref", m.group(1), _ref_value(after, m.group(1)), _ref_value(before, m.group(1))))
        elif (m := _FILE.match(change)) and not _is_lock(m.group(1)):
            items.append(Restored("file", m.group(1), _file_value(after, m.group(1)), _file_value(before, m.group(1))))
    return items


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

def confirm_locks(repo: Repo, changes: Sequence[str]) -> list[str]:
    """A new `*.lock` may be a live git's for a moment (PD-66 (ff)): wait LOCK_GRACE and keep as changes only the
    locks that are still the very same file afterwards; the others were not stale."""
    def mark(name: str):
        try:
            st = (repo.common_dir / name).stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns)
    locks = {}
    for c in changes:
        m = _FILE.match(c)
        if m and _is_lock(m.group(1)):
            locks[c] = m.group(1)
    if not locks:
        return list(changes)
    seen = {c: mark(name) for c, name in locks.items()}
    time.sleep(LOCK_GRACE)
    return [c for c in changes if c not in locks or (seen[c] is not None and mark(locks[c]) == seen[c])]


def _quarantine(quarantine: Path | None, name: str, source: Path) -> None:
    """Copy what is on disk now to the quarantine directory (a link: its target text). A failure is not an
    obstacle to the restore."""
    if quarantine is None:
        return
    try:
        target_text = platform.read_link(source)
        data = _LINK + target_text.encode("utf-8", "surrogateescape") if target_text is not None else source.read_bytes()
        target = quarantine / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError:
        pass


def _under_link(common: Path, path: Path) -> bool:
    """A parent of `path` inside the git directory is a symbolic link."""
    parent = path.parent
    while parent != common and common in parent.parents:
        if platform.read_link(parent) is not None:
            return True
        parent = parent.parent
    return False


def _replace(source, target) -> None:
    """os.replace, retried a few times: on Windows a reader holding the target open makes it fail."""
    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.1)


def _replace_file(path: Path, data: bytes, mode: int | None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")   # a name nobody can plan
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        platform.set_file_mode(Path(tmp), mode)
        _replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _restore_config(repo: Repo, path: Path, name: str, before: Snapshot, after: Snapshot) -> bool:
    """Git's own protocol: write `<file>.lock` (O_EXCL: someone else's lock means skip) and rename it over the
    file. The runner's tracking entries are what they are at the end of the stage, removals included."""
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
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
        _replace(lock, path)
        done = True
    finally:
        if not done:
            lock.unlink(missing_ok=True)
    return True


def _restore_file(repo: Repo, name: str, how: str, before: Snapshot, after: Snapshot,
                  quarantine: Path | None) -> bool:
    path = repo.common_dir / name
    if _is_lock(name):   # a lock that was not there before and is still there: stale (the caller waited)
        path.unlink(missing_ok=True)
        return True
    if _under_link(repo.common_dir, path):   # the agent linked the directory away: its files are not ours
        return how == "created"              # (a file it hides is gone with the directory the link replaced)
    try:
        if name in _COMMON_DIRS and path.is_dir() and not path.is_symlink():
            # a real directory where the owner had a link (or none): move it out of the way, keep a copy
            if quarantine is not None:
                shutil.copytree(path, quarantine / name, symlinks=True, dirs_exist_ok=True,
                                ignore_dangling_symlinks=True)
            shutil.rmtree(path)
            if how == "deleted" and before.files[name].startswith(_LINK):
                return platform.make_file_link(path, before.files[name][len(_LINK):].decode("utf-8", "surrogateescape"))
            return True
        if name in _COMMON_DIRS and how == "created":   # a link where a directory was: back to a directory
            _quarantine(quarantine, name, path)
            path.unlink(missing_ok=True)
            path.mkdir(parents=True, exist_ok=True)
            return True
        if how == "created":
            _quarantine(quarantine, name, path)
            path.unlink(missing_ok=True)   # a link is removed itself, never followed
            return True
        if how == "changed":
            _quarantine(quarantine, name, path)
            if after.files.get(name) == before.files[name]:
                if platform.read_link(path) is None:
                    platform.set_file_mode(path, before.modes.get(name))   # a mode-only change
                return True
        if _is_config(name):
            # a configuration file may be a link by the owner's own doing: write through it only when it was
            # that very link before the stage
            if (target := before.links.get(name)) is not None and after.links.get(name) == target:
                path = path.resolve()
            return _restore_config(repo, path, name, before, after)
        was = before.files[name]
        if was.startswith(_LINK):
            path.unlink(missing_ok=True)
            return platform.make_file_link(path, was[len(_LINK):].decode("utf-8", "surrogateescape"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(path, was, before.modes.get(name))
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
            quarantine: Path | None = None, slot_keep: Sequence[str] = (),
            record: Callable[[list[Restored]], None] | None = None) -> list[str]:
    """... `record` is told what was put back before the process-wide lock is released, so no other stage's
    `take` can come between the restore and its record (PD-66 (x))."""
    with CHECK_LOCK:
        left = _restore(repo, before, after, changes, quarantine, slot_keep)
        if record is not None:
            record(restored_items(before, after, [c for c in changes if c not in left]))
        return left


def _restore(repo: Repo, before: Snapshot, after: Snapshot, changes: Sequence[str],
             quarantine: Path | None, slot_keep: Sequence[str]) -> list[str]:
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

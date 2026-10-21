"""Protected paths (spec §4, PD-47): a task that changes agent or CI configuration is reviewed on the strong model
and never merges on its own.

Entries come from the `protected_paths` setting (+ `templates_dir`) and are derived from the commands that run on
the runner's machine: files named by a list-form `commands.verify` / `commands.setup`, and every
`commands.setup_inputs` entry. Derived entries are root-anchored exact paths.
"""
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass

from artel.errors import GitError
from artel.gitutil import INTEGRATION, Repo

SHOWN = 3   # paths named in a summary before "+N more"


@dataclass(frozen=True)
class Entry:
    path: str
    exact: bool     # derived: root-anchored, the whole path
    source: str     # "protected_paths" | "templates_dir" | "commands.verify" | "commands.setup" | "commands.setup_inputs"


@dataclass(frozen=True)
class Hit:
    path: str
    source: str
    note: str = ""   # also used by the test guard (Task 38)


def _parts(path: str) -> list[str]:
    """`/`-separated components, case folded; `\\` read as `/`, empty and `.` components dropped."""
    return [p for p in path.replace("\\", "/").casefold().split("/") if p not in ("", ".")]


def _repo_relative(repo: Repo, arg: str) -> str | None:
    """A command argument as a repository-relative `/` path, or None when it cannot name a file there."""
    text = arg.strip().replace("\\", "/")
    if not text or "\x00" in text:
        return None
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):   # absolute: only inside the repository
        text = posixpath.normpath(text)
        for root in {repo.root.resolve().as_posix(), repo.root.as_posix()}:
            prefix = posixpath.normpath(root).rstrip("/") + "/"
            if text.casefold().startswith(prefix.casefold()):   # case-insensitive file systems
                text = text[len(prefix):]
                break
        else:
            return None
    text = posixpath.normpath(text)
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or parts[0] == ".." or ".." in parts:
        return None
    return "/".join(parts)


def _is_blob(repo: Repo, tip: str, path: str) -> bool:
    """True for a regular file at `tip`. `ls-tree` prints nothing for a missing path, `tree` for a directory and
    `commit` for a submodule (its object is not in this repository); any git failure raises GitError."""
    listed = repo.git("--literal-pathspecs", "ls-tree", tip, "--", path)   # `:!x` in an argument is no magic
    fields = listed.stdout.split(None, 2)
    return len(fields) >= 2 and fields[1] == "blob"


def entries(repo: Repo, cfg) -> tuple[Entry, ...]:
    found: dict[tuple[str, bool], Entry] = {}

    def add(path: str, exact: bool, source: str) -> None:
        if path:
            found.setdefault((path, exact), Entry(path, exact, source))

    for item in cfg["protected_paths"]:
        add(item, False, "protected_paths")
    if cfg["protected_paths"]:
        templates = _repo_relative(repo, cfg["templates_dir"])   # an absolute templates_dir inside the repository
        if templates is not None:
            add(templates, False, "templates_dir")
    tip = repo.rev(INTEGRATION)
    for key in ("commands.verify", "commands.setup"):
        command = cfg[key]
        if tip is None or not isinstance(command, (list, tuple)):
            continue   # string-form commands are not parsed
        for arg in command:
            rel = _repo_relative(repo, str(arg))
            if rel is not None and _is_blob(repo, tip, rel):
                add(rel, True, key)
    for item in cfg["commands.setup_inputs"]:
        rel = _repo_relative(repo, item)
        if rel is not None:
            add(rel, True, "commands.setup_inputs")
    return tuple(sorted(found.values(), key=lambda e: (e.path, e.exact)))


def matches(path: str, entry: Entry) -> bool:
    got, want = _parts(path), _parts(entry.path)
    if not want:
        return False
    if entry.exact:
        return got == want
    if len(want) == 1:                  # no inner "/": that name anywhere in the tree
        return want[0] in got
    return got[:len(want)] == want      # an inner "/": a root-anchored path-component prefix


def changed_paths(repo: Repo, tip: str) -> list[str]:
    """Raises GitError when git fails (a missing `tip` included)."""
    result = repo.git("diff", "--name-only", "--no-renames", "-z", f"{INTEGRATION}...{tip}")
    return sorted(p for p in result.stdout.split("\x00") if p)


GIT_ERROR = "<git error>"


def hits(repo: Repo, cfg, tip: str) -> list[Hit]:
    """Fails closed: a git failure while deciding is one hit, so the task goes to a human and a strong review."""
    try:
        found = entries(repo, cfg)
        if not found:
            return []
        out = []
        for path in changed_paths(repo, tip):
            for entry in found:
                if matches(path, entry):
                    out.append(Hit(path, entry.source))
                    break
        return out
    except GitError as e:
        why = next((line.strip() for line in e.stderr.splitlines() if line.strip()), "no message")
        return [Hit(GIT_ERROR, "protected_paths", f"git failed while looking for protected paths: {why}")]


def summary(hits: Sequence[Hit]) -> str:
    names = [h.path for h in hits]
    text = ", ".join(names[:SHOWN])
    return text + (f" +{len(names) - SHOWN} more" if len(names) > SHOWN else "")

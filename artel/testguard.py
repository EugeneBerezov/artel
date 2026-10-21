"""Test guard (spec §4, PD-64): what the runner can tell about a task's test changes without spending tokens.

Hard hits (they make a green verify ask a human): a deleted or renamed test file, fewer assertion lines, an
added disabling marker, a test id that passed on the integration tip but not in the task's verify, a test named
by the task text (`Tests:` lines) that did not pass. Soft hits (the round-1 review runs on the strong model, the
merge does not ask): any other changed line in a test path, a binary change, a changed test setup file.

The ids of a passed test come from the verify log (`verify_test_id_regex`). They are kept per sha, only for
green verifies, in `<git-common-dir>/artel/verify-ids/<sha>.txt`; meta `base_verify` is a separate record, the
base-check cache (PD-68).
"""
import fnmatch
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from artel import protect
from artel.errors import GitError
from artel.gitutil import INTEGRATION, Repo
from artel.protect import Hit

SOURCES = ("test_paths", "test_assert_markers", "test_markers")   # settings behind a diff hit
VERIFY = "(verify)"          # the path of a hit that belongs to the verify run, not to a file
REQUIRED_SOURCE = "Tests: line"
KEEP_IDS = 20                # id files kept
SHOWN = 3                    # ids or reasons named before "+N more"
_META = "base_verify"
_COMMENT = ("#", "//", "/*", "*")
_TEST_FILE = ("test_*", "*_test.*", "*.test.*", "*.spec.*")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_TESTS_LINE = re.compile(r"^[ \t]*[-*]?[ \t]*Tests:(.*)$", re.MULTILINE)
_NAME = re.compile(r"[\w.:/\[\]-]+")
_UNITTEST_ID = re.compile(r"^\S+ \(([\w.]+)\)$")
_LOAD_TESTS = re.compile(r"^\s*def load_tests\(")


# --- which paths --------------------------------------------------------------------------------------------

def path_matches(path: str, entry: str) -> bool:
    if "*" in entry or "?" in entry:
        name = PurePosixPath(path.replace("\\", "/")).name
        return fnmatch.fnmatchcase(name.casefold(), entry.casefold())
    return protect.matches(path, protect.Entry(entry, exact=False, source="test_paths"))


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.casefold()
    if name in ("__init__.py", "conftest.py"):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _TEST_FILE)


# --- the diff -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Change:
    path: str
    status: str                 # "A" | "M" | "D" (a rename is D + A, --no-renames)
    is_test: bool
    binary: bool
    removed: tuple[str, ...]    # content lines, without the diff's `-`
    added: tuple[str, ...]


_ESCAPE = "surrogateescape"   # a path git prints that is not UTF-8 must still reach `git diff` as it was


def _shown(path: str) -> str:
    """A path for people and the journal: surrogate escapes cannot be encoded, so they become U+FFFD."""
    return path.encode("utf-8", _ESCAPE).decode("utf-8", "replace")
_CHUNK = 100                  # paths per `git diff` command line (Windows limits its length)


def _statuses(repo: Repo, tip: str) -> list[tuple[str, str]]:
    out = repo.git("diff", "--raw", "--no-renames", "--no-ext-diff", "-z", f"{INTEGRATION}...{tip}",
                   errors=_ESCAPE).stdout
    parts = out.split("\0")
    found = []
    for i in range(0, len(parts) - 1, 2):   # `:mode mode sha sha S` NUL path NUL; no renames, so always pairs
        meta, path = parts[i], parts[i + 1]
        if meta.startswith(":") and path:
            found.append((meta.rsplit(" ", 1)[-1][:1], path))
    return found


def _sections(text: str) -> list[tuple[bool, list[str], list[str]]]:
    """A `git diff -U0` output as (binary, removed lines, added lines) per file section. Lines are read by
    position, not by prefix alone: a `+++` before the first hunk is a header, a `+++` after it is content."""
    found: list[tuple[bool, list[str], list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("diff --git "):   # content lines carry a +, - or \ prefix: this is always a header
            found.append((False, [], []))
            in_hunk = False
            continue
        if not found:
            continue
        binary, removed, added = found[-1]
        if not in_hunk:
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith("Binary files "):
                found[-1] = (True, removed, added)
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return found


def _merged(sections: list[tuple[bool, list[str], list[str]]]) -> tuple[bool, list[str], list[str]]:
    """All the sections git printed for one path (a type change prints a delete and an add)."""
    return (any(s[0] for s in sections), [line for s in sections for line in s[1]],
            [line for s in sections for line in s[2]])


def _patches(repo: Repo, tip: str, paths: list[str]) -> dict[str, tuple[bool, list[str], list[str]]]:
    """One `git diff -U0` per chunk of paths, one section per path in git's order. When the section count does not
    match (a type change prints two) every path is diffed on its own. The text is read lossily: its paths are
    never used, only its lines."""
    def diff(chosen: list[str]) -> str:
        return repo.git("--literal-pathspecs", "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff",
                        "--no-textconv", "--submodule=short", f"{INTEGRATION}...{tip}", "--", *chosen).stdout

    found: dict[str, tuple[bool, list[str], list[str]]] = {}
    for start in range(0, len(paths), _CHUNK):
        chunk = paths[start:start + _CHUNK]
        sections = _sections(diff(chunk))
        if len(sections) != len(chunk):
            sections = [_merged(_sections(diff([path]))) for path in chunk]
        found.update(zip(chunk, sections))
    return found


@dataclass(frozen=True)
class Scan:
    """The test-path part of a task's diff, read once per stage and shared by hard_hits and soft_hits."""
    changes: tuple[_Change, ...] = ()
    error: GitError | None = None


def scan(repo: Repo, cfg, tip: str) -> Scan:
    """The changed paths that match `test_paths`, with their lines. Never raises: a git failure is in `error`."""
    entries = cfg["test_paths"]
    if not entries:
        return Scan()
    try:
        matching = [(status, path) for status, path in _statuses(repo, tip)
                    if any(path_matches(_shown(path), entry) for entry in entries)]
        texts = _patches(repo, tip, [path for status, path in matching if status != "D"])
    except GitError as error:
        return Scan(error=error)
    found = []
    for status, path in matching:
        binary, removed, added = (False, [], []) if status == "D" else texts[path]
        found.append(_Change(_shown(path), status, is_test_file(path), binary, tuple(removed), tuple(added)))
    return Scan(tuple(found))


def _git_error(error: GitError) -> Hit:
    why = next((line.strip() for line in error.stderr.splitlines() if line.strip()), "no message")
    return Hit(protect.GIT_ERROR, "test_paths", f"git failed while looking at test changes: {why}")


def _assertions(lines: Iterable[str], markers: Sequence[str]) -> int:
    count = 0
    for line in lines:
        if line.strip().startswith(_COMMENT):   # a commented-out assertion is gone, a commented one is not new
            continue
        if any(marker in line for marker in markers):
            count += 1
    return count


def hard_hits(repo: Repo, cfg, tip: str, scanned: Scan | None = None) -> list[Hit]:
    """Fails closed: a git failure is one hit. `scanned`: the stage's shared `scan`."""
    scanned = scanned or scan(repo, cfg, tip)
    if scanned.error is not None:
        return [_git_error(scanned.error)]
    markers = [re.compile(pattern) for pattern in cfg["test_markers"]]
    found = []
    for change in scanned.changes:
        if change.status == "D":
            found.append(Hit(change.path, "test_paths", "deleted"))
            continue
        fewer = (_assertions(change.removed, cfg["test_assert_markers"])
                 - _assertions(change.added, cfg["test_assert_markers"]))
        if fewer > 0:
            found.append(Hit(change.path, "test_assert_markers", f"{fewer} fewer assertion lines"))
        seen: set[int] = set()
        for line in change.added:   # added lines only: removing a marker is free
            for i, marker in enumerate(markers):
                if i not in seen and marker.search(line):
                    seen.add(i)
                    found.append(Hit(change.path, "test_markers", f'adds "{line.strip()[:60]}"'))
    return found


def soft_hits(repo: Repo, cfg, tip: str, scanned: Scan | None = None) -> list[Hit]:
    scanned = scanned or scan(repo, cfg, tip)
    found = []
    for change in scanned.changes:   # a git failure leaves no changes: hard_hits reports it
        if change.status == "D":
            continue
        if not change.is_test:
            found.append(Hit(change.path, "test_paths", "test setup changed"))
        elif change.binary and change.status != "A":
            found.append(Hit(change.path, "test_paths", "binary change"))
        elif change.removed:
            found.append(Hit(change.path, "test_paths", f"{len(change.removed)} lines changed"))
        if change.is_test and any(_LOAD_TESTS.match(line) for line in change.added):
            found.append(Hit(change.path, "test_paths", "adds load_tests"))   # can change which tests run
    return found


# --- test ids -------------------------------------------------------------------------------------------------

def passed_ids(text: str, patterns: Sequence[str]) -> frozenset[str] | None:
    """The ids of passed tests: group 1 of every match of every pattern (MULTILINE). None when there are no
    patterns or nothing matched. Whitespace inside an id is collapsed so both sides of a comparison agree."""
    found = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            test = " ".join((match.group(1) or "").split())
            if test:
                found.add(test)
    return frozenset(found) or None


def ids_from_log(log_path: Path, patterns: Sequence[str]) -> frozenset[str] | None:
    """Read the log as written: `newline=""` keeps a CRLF the patterns have to expect."""
    if not patterns:
        return None
    try:
        with open(log_path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    return passed_ids(text, patterns)


def _ids_path(repo: Repo, sha: str) -> Path | None:
    return repo.artel_dir / "verify-ids" / f"{sha}.txt" if _SHA.fullmatch(sha) else None


def write_ids(repo: Repo, sha: str, ids: frozenset[str]) -> None:
    """The id baseline of `sha`, written once; the newest KEEP_IDS files are kept. Raises OSError."""
    path = _ids_path(repo, sha)
    if path is None or path.exists():   # the first green record of a sha stands: a verify that merged nothing
        return                          # (commit == tip) must not shrink the tip's baseline
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")   # a crash leaves a stray .tmp, never a truncated baseline
    with open(partial, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"{test}\n" for test in sorted(ids)))
    partial.replace(path)
    tip = repo.rev(INTEGRATION)   # the baseline the id rule reads next: task verifies must not push it out
    kept = 1 + (tip is not None and tip != sha and (path.parent / f"{tip}.txt").exists())   # this file, the tip's
    older = sorted((p for p in path.parent.glob("*.txt") if p != path and p.stem != tip),
                   key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    for stale in older[KEEP_IDS - kept:]:
        stale.unlink(missing_ok=True)


def base_ids(repo: Repo, sha: str) -> frozenset[str] | None:
    path = _ids_path(repo, sha)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    return frozenset(line for line in text.split("\n") if line) or None


def read_base(board) -> dict | None:
    raw = board.get_meta(_META)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def write_base(board, repo: Repo, *, sha: str, green: bool, at: float, log: str,
               ids: frozenset[str] | None) -> None:
    """The only writer of meta `base_verify` (the base-check cache) and, for a green result with ids, of the id
    baseline of `sha`. A red result never touches the baseline."""
    board.set_meta(_META, json.dumps({"sha": sha, "green": green, "at": at, "log": log}))
    if green and ids is not None:
        write_ids(repo, sha, ids)


def _named(names: Sequence[str]) -> str:
    shown = ", ".join(names[:SHOWN])
    return shown + (f" +{len(names) - SHOWN} more" if len(names) > SHOWN else "")


def id_hit(repo: Repo, board, cfg, result) -> Hit | None:
    """The id rule: a test that passed on the integration tip the verify merged must pass here too. No ids
    recorded for that tip (or no regex) → no check, see `id_check_off`."""
    if not cfg["verify_test_id_regex"] or not cfg["commands.verify"] or not result.base_sha:
        return None   # nothing ran with an empty verify: the empty-verify gate says so
    before = base_ids(repo, result.base_sha)
    if before is None:
        return None
    missing = sorted(before - (result.ids or frozenset()))
    if not missing:
        return None
    return Hit(VERIFY, "verify_test_id_regex",
               f"{len(missing)} tests passed on {result.base_sha[:12]} but not here: {_named(missing)}")


def id_check_off(repo: Repo, cfg, result) -> str | None:
    """The VERDICT text when the id rule could not run, else None."""
    if not cfg["verify_test_id_regex"] or not cfg["commands.verify"] or not result.base_sha:
        return None
    if base_ids(repo, result.base_sha) is not None:
        return None
    return f"test-id check off: no ids recorded for {result.base_sha[:12]} (verify_test_id_regex)"


# --- required tests (the task text) ---------------------------------------------------------------------------

def _items(body: str) -> tuple[list[str], list[str]]:
    """(names, dropped items) of the `Tests:` lines. A line is optionally indented and bulleted (`-`, `*`); the
    prefix is case-sensitive. Parenthesised remarks are cut before the commas are split; each item loses
    surrounding backticks and quotes and is reduced to its first test-name token."""
    names: list[str] = []
    dropped: list[str] = []
    for match in _TESTS_LINE.finditer(body or ""):
        remark = match.group(1)
        while True:   # cut parenthesised remarks, nested ones too, before splitting at the commas
            plain = re.sub(r"\([^()]*\)", " ", remark)
            if plain == remark:
                break
            remark = plain
        for item in remark.split(","):
            item = item.strip()
            if not item:
                continue
            token = _NAME.search(item)
            name = token.group(0).rstrip(".:") if token else ""   # `test_b.` and `test_a:` end a sentence
            if not name:
                dropped.append(item)
            elif name not in names:
                names.append(name)
    return names, dropped


def required_tests(body: str) -> list[str]:
    return _items(body)[0]


def required_ignored(body: str) -> list[str]:
    """The `Tests:` items that named no test: the caller journals them, they are no requirement."""
    return _items(body)[1]


def missing_required(names: Sequence[str], passed_ids: Iterable[str]) -> list[str]:
    """The names no passed id satisfies: an id equal to the name or ending with `.` or `::` + name, optionally
    followed by a `[...]` parameter suffix. A unittest id `test_x (pkg.mod.Case.test_x)` also counts as its
    dotted part."""
    candidates = set()
    for test in passed_ids:
        candidates.update({test, test.split("[", 1)[0]})   # `t.py::test_x[1]`, `test_x[a-b]`, a cut `test_x[a`
        inner = _UNITTEST_ID.match(test)
        if inner:
            dotted = inner.group(1)
            candidates.update({dotted, f"{dotted}.{test.split()[0]}"})   # Python < 3.11: `test_x (pkg.mod.Case)`
    return [name for name in names
            if not any(c == name or c.endswith("." + name) or c.endswith("::" + name) for c in candidates)]


def required_hits(names: Sequence[str], ids: frozenset[str] | None) -> list[Hit]:
    if not names:
        return []
    if not ids:
        return [Hit(VERIFY, "verify_test_id_regex", "required tests unconfirmed: verify printed no test ids")]
    missing = missing_required(names, ids)
    if not missing:
        return []
    return [Hit(VERIFY, REQUIRED_SOURCE, f"required tests did not pass: {_named(missing)}")]

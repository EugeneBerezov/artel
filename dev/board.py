#!/usr/bin/env python3
"""Task board and journal for parallel peer Claude Code sessions.

Dev tool, not part of the artel package. Python >= 3.11, stdlib only, Linux.
Storage: <git-common-dir>/artel-dev/ (shared by all worktrees of the repo).
See dev/README.md for the commands.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAIN_BRANCH = "main"
MAX_TEXT_BYTES = 400
MAX_BASIS_BYTES = 200
MAX_FAILS = 2
TEST_TIMEOUT = 20 * 60
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
KINDS = ("FACT", "FAIL", "NOTE", "SPEC-GAP")
GITHUB_REPO = "EugeneBerezov/artel"
TOKEN_PATH = Path.home() / ".config" / "artel-dev" / "github-token"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

EXIT_ERROR = 1
EXIT_NOTHING = 3
EXIT_ENV = 4  # ci: token missing / HTTP failure


class BoardError(Exception):
    pass


# --------------------------------------------------------------------------
# Plan parsing
# --------------------------------------------------------------------------

TASK_RE = re.compile(r"^### Task (\d+):\s*(.+?)\s*$")
HEADING_RE = re.compile(r"^#{1,3} ")
WAVE_ROW_RE = re.compile(r"^\|\s*W(\d+)\s*\|(.*)\|\s*$")
DEPENDS_RE = re.compile(r"^\*\*Depends on:?\*\*:?\s*(.+)$", re.IGNORECASE)


def _clean_title(raw):
    title = re.sub(r"\s*(?:—\s*)?\*\*RISKY[^*]*\*\*", "", raw)
    return title.strip().rstrip("—-").strip()


def _wave_table(lines):
    """From the wave table: ({task_id: wave}, [(earlier, later)], {bold ids})."""
    waves, chains, bold = {}, [], set()
    for line in lines:
        m = WAVE_ROW_RE.match(line)
        if not m:
            continue
        wave = int(m.group(1))
        for group in m.group(2).split("·"):
            prev = None
            for seg in group.split("→"):
                n = re.match(r"[\s*]*(\d+)\b", seg)
                if not n:
                    continue
                tid = int(n.group(1))
                waves[tid] = wave
                if seg.strip().startswith("**"):
                    bold.add(tid)
                if prev is not None:
                    chains.append((prev, tid))
                prev = tid
    return waves, chains, bold


def parse_plan(text):
    lines = text.splitlines()
    heads = []  # (line number, task id, raw title)
    boundaries = []  # line numbers of any heading of level <= 3 (outside fences)
    in_fence = False
    for no, line in enumerate(lines, 1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = TASK_RE.match(line)
        if m:
            heads.append((no, int(m.group(1)), m.group(2)))
        if HEADING_RE.match(line):
            boundaries.append(no)
    waves, chains, bold = _wave_table(lines)
    ids = [h[1] for h in heads]
    if not heads:
        raise BoardError("no '### Task N:' headings found")
    if len(set(ids)) != len(ids):
        raise BoardError("duplicate task headings in plan")
    if set(ids) != set(waves):
        raise BoardError(
            "wave table and task headings disagree: only in headings %s, "
            "only in table %s" % (sorted(set(ids) - set(waves)),
                                  sorted(set(waves) - set(ids))))
    by_wave = {}
    for tid, w in waves.items():
        by_wave.setdefault(w, []).append(tid)
    tasks = []
    for no, tid, raw in heads:
        later = [b for b in boundaries if b > no]
        end = (later[0] - 1) if later else len(lines)
        while end > no and lines[end - 1].strip() in ("", "---"):
            end -= 1
        explicit = None
        in_fence = False
        for line in lines[no:end]:
            if line.startswith("```"):
                in_fence = not in_fence
            m = None if in_fence else DEPENDS_RE.match(line)
            if m:
                explicit = sorted({int(x) for x in re.findall(r"\d+",
                                                              m.group(1))})
        wave = waves[tid]
        if explicit is not None:
            deps = explicit
        else:
            lower = [w for w in by_wave if w < wave]
            deps = set(by_wave[max(lower)]) if lower else set()
            deps |= {a for a, b in chains if b == tid}
            deps = sorted(deps)
        missing = [d for d in deps if d not in waves]
        if missing:
            raise BoardError("task %d depends on unknown task(s) %s"
                             % (tid, missing))
        tasks.append({
            "id": tid, "title": _clean_title(raw), "wave": wave,
            "deps": deps, "risky": "**RISKY" in raw or tid in bold,
            "controller": "controller task" in raw.lower(),
            "plan_lines": [no, end],
            "state": "open", "session": None, "claimed_at": None,
            "fails": 0, "merge_sha": None,
        })
    return tasks


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, cwd, timeout=None):
    """Run a command, merging stderr into stdout. Returns (rc, output)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, text=True, errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return 124, out + "\n[timed out after %ss]" % timeout
    return r.returncode, r.stdout


def git(cwd, *args, timeout=None):
    return run(["git", *args], cwd, timeout)


class Store:
    def __init__(self, repo):
        rc, out = git(repo, "rev-parse", "--path-format=absolute",
                      "--git-common-dir")
        if rc != 0:
            raise BoardError("not a git repository: %s" % repo)
        self.repo = Path(repo)
        self.dir = Path(out.strip()) / "artel-dev"
        self.board_path = self.dir / "board.json"
        self.journal_path = self.dir / "journal.md"
        self.cursor_dir = self.dir / "cursors"

    @contextlib.contextmanager
    def lock(self, name="lock"):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.dir / name, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def exists(self):
        return self.board_path.exists()

    def load(self):
        try:
            return json.loads(self.board_path.read_text())
        except FileNotFoundError:
            raise BoardError("no board; run: board.py init --from docs/plan.md")

    def save(self, board):
        tmp = self.board_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(board, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.board_path)

    def journal(self, session, kind, tid, text, basis=None):
        text = " ".join(str(text).split())
        line = "- %s %s %s T%s: %s" % (now_iso(), session or "-", kind, tid,
                                       text)
        if basis:
            line += " [basis: %s]" % " ".join(str(basis).split())
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def find_task(board, tid):
    for t in board["tasks"]:
        if t["id"] == tid:
            return t
    raise BoardError("no such task: T%s" % tid)


def check_session(s):
    if not s or not SESSION_RE.match(s):
        raise BoardError("bad session name %r (use letters, digits, . _ -)"
                         % (s,))
    return s


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args):
    store = Store(args.repo)
    plan_path = Path(args.repo) / args.plan
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        raise BoardError("cannot read plan: %s" % e)
    tasks = parse_plan(text)
    with store.lock():
        if store.exists() and not args.force:
            raise BoardError("board already exists (use --force to replace "
                             "it, which resets all task state)")
        store.save({"version": 1, "source": args.plan, "created": now_iso(),
                    "tasks": tasks})
    print("initialised %d tasks from %s" % (len(tasks), args.plan))


def describe(board, t, session):
    lo, hi = t["plan_lines"]
    return "\n".join([
        "id: %d" % t["id"],
        "title: %s" % t["title"],
        "risky: %s" % ("yes" if t["risky"] else "no"),
        "wave: %d" % t["wave"],
        "plan: %s:%d-%d" % (board["source"], lo, hi),
        "branch: dev/T%d" % t["id"],
        "worktree: .claude/worktrees/%s-T%d" % (session, t["id"]),
    ])


def cmd_claim(args):
    session = check_session(args.session)
    store = Store(args.repo)
    with store.lock():
        board = store.load()
        tasks = board["tasks"]
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            if t["state"] == "claimed" and t["session"] == session:
                print(describe(board, t, session))
                return 0
        cands = [t for t in tasks if t["state"] == "open"
                 and not t["controller"]
                 and all(by_id[d]["state"] == "done" for d in t["deps"])]
        if not cands:
            print(_nothing_claimable(tasks, by_id), file=sys.stderr)
            return EXIT_NOTHING
        t = min(cands, key=lambda x: (x["wave"], x["id"]))
        t["state"], t["session"], t["claimed_at"] = "claimed", session, now_iso()
        store.save(board)
        store.journal(session, "CLAIM", t["id"], t["title"])
        print(describe(board, t, session))
    return 0


def _nothing_claimable(tasks, by_id):
    open_ = [t for t in tasks if t["state"] == "open"]
    held = {}
    for t in open_:
        if t["controller"]:
            continue
        for d in t["deps"]:
            dep = by_id[d]
            if dep["state"] == "claimed":
                held.setdefault(dep["id"], dep["session"])
    parts = []
    if held:
        parts.append("nothing claimable: tasks are waiting on dependencies "
                     "held by others (%s)" % ", ".join(
                         "T%d by %s" % (i, s) for i, s in sorted(held.items())))
    else:
        parts.append("nothing claimable")
    ctrl = [t["id"] for t in open_ if t["controller"]]
    if ctrl:
        parts.append("controller tasks (not claimable by peers, mark with "
                     "'done'): %s" % ", ".join("T%d" % i for i in ctrl))
    stuck = [t["id"] for t in tasks if t["state"] == "stuck"]
    if stuck:
        parts.append("stuck: %s" % ", ".join("T%d" % i for i in stuck))
    if not open_ and not held:
        parts.append("no open tasks left")
    return "; ".join(parts)


def _reopen(store, tid, kind, reason, session_arg, count_fail):
    with store.lock():
        board = store.load()
        t = find_task(board, tid)
        if t["state"] == "done":
            raise BoardError("T%d is already done" % tid)
        who = session_arg or t["session"] or "-"
        t["state"], t["session"], t["claimed_at"] = "open", None, None
        text = reason
        if count_fail:
            t["fails"] += 1
            if t["fails"] >= MAX_FAILS:
                t["state"] = "stuck"
                text += " (stuck after %d fails)" % t["fails"]
        store.save(board)
        store.journal(who, kind, tid, text)
        return t["state"]


def cmd_release(args):
    state = _reopen(Store(args.repo), args.id, "RELEASE",
                    args.reason or "released", None, False)
    print("T%d -> %s" % (args.id, state))


def cmd_fail(args):
    state = _reopen(Store(args.repo), args.id, "FAIL", args.reason, None, True)
    print("T%d -> %s" % (args.id, state))


def cmd_done(args):
    store = Store(args.repo)
    with store.lock():
        board = store.load()
        t = find_task(board, args.id)
        if t["state"] == "done":
            raise BoardError("T%d is already done" % args.id)
        who = args.session or t["session"] or "-"
        t["state"], t["merge_sha"] = "done", args.sha
        t["session"], t["claimed_at"] = None, None
        store.save(board)
        store.journal(who, "DONE", args.id, args.note or "marked done",
                      basis=args.sha)
    print("T%d -> done" % args.id)


def _check_bytes(name, value, limit):
    n = len(value.encode("utf-8"))
    if n > limit:
        raise BoardError("%s is %d bytes, limit %d" % (name, n, limit))


def cmd_note(args):
    text = " ".join(args.text.split())
    if not text:
        raise BoardError("empty note text")
    _check_bytes("text", text, MAX_TEXT_BYTES)
    if args.basis:
        _check_bytes("basis", args.basis, MAX_BASIS_BYTES)
    session = check_session(args.session or os.environ.get(
        "ARTEL_DEV_SESSION") or "unknown")
    store = Store(args.repo)
    with store.lock():
        find_task(store.load(), args.id)
        store.journal(session, args.kind, args.id, text, args.basis)


def cmd_tail(args):
    session = check_session(args.session)
    store = Store(args.repo)
    with store.lock():
        store.cursor_dir.mkdir(parents=True, exist_ok=True)
        cursor = store.cursor_dir / session
        try:
            data = store.journal_path.read_bytes()
        except FileNotFoundError:
            data = b""
        start = 0
        if not args.all:
            try:
                start = int(cursor.read_text().strip() or 0)
            except (FileNotFoundError, ValueError):
                start = 0
            if start > len(data):
                start = 0
        sys.stdout.write(data[start:].decode("utf-8", errors="replace"))
        if not args.all:
            cursor.write_text(str(len(data)))


def cmd_status(args):
    store = Store(args.repo)
    with store.lock():
        board = store.load()
    rows = [("id", "wave", "title", "state", "session", "risky")]
    for t in board["tasks"]:
        title = t["title"]
        if len(title) > 44:
            title = title[:43] + "…"
        flags = ("yes" if t["risky"] else "") + (
            " ctrl" if t["controller"] else "")
        rows.append(("T%d" % t["id"], "W%d" % t["wave"], title, t["state"],
                     t["session"] or "-", flags.strip() or "-"))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    counts = {}
    for t in board["tasks"]:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    print()
    print("%s (%d total)" % (", ".join(
        "%s %d" % (s, counts.get(s, 0))
        for s in ("open", "claimed", "done", "stuck")), len(board["tasks"])))


def main_worktree(repo):
    rc, out = git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        raise BoardError("git worktree list failed: %s" % out.strip())
    for line in out.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    raise BoardError("no worktrees found")


def _tail_lines(text, n=40):
    return "\n".join(text.strip().splitlines()[-n:])


def cmd_integrate(args):
    store = Store(args.repo)
    # Serialises integrations without blocking claim/note/tail for up to
    # TEST_TIMEOUT: the board lock is only taken for the final bookkeeping.
    with store.lock("integrate.lock"):
        with store.lock():
            task = find_task(store.load(), args.id)
            if task["state"] == "done":
                raise BoardError("T%d is already done" % args.id)
            title, session = task["title"], task["session"] or "-"
        main = main_worktree(args.repo)
        rc, out = git(main, "symbolic-ref", "--short", "-q", "HEAD")
        if rc != 0 or out.strip() != MAIN_BRANCH:
            raise BoardError("main worktree %s is not on '%s' (on '%s')"
                             % (main, MAIN_BRANCH, out.strip() or "detached"))
        rc, out = git(main, "status", "--porcelain", "--untracked-files=no")
        if rc != 0 or out.strip():
            raise BoardError("main worktree %s is not clean:\n%s"
                             % (main, _tail_lines(out, 15)))
        rc, out = git(main, "rev-parse", "--verify", "-q",
                      args.branch + "^{commit}")
        if rc != 0:
            raise BoardError("unknown branch %r" % args.branch)
        rc, head = git(main, "rev-parse", "HEAD")
        head = head.strip()

        def rollback(why, output):
            git(main, "merge", "--abort")
            git(main, "reset", "--hard", head)
            print("integrate T%d failed: %s\n%s" % (
                args.id, why, _tail_lines(output)), file=sys.stderr)
            return EXIT_ERROR

        rc, out = git(main, "merge", "--no-ff", args.branch, "-m",
                      "Merge T%d: %s" % (args.id, title), "-m", TRAILER)
        if rc != 0:
            why = ("merge conflict" if "onflict" in out else "merge failed")
            return rollback(why, out)
        rc, new = git(main, "rev-parse", "HEAD")
        if new.strip() == head:
            return rollback("nothing to merge (branch already in main)", out)
        if (Path(main) / "tests").is_dir():
            rc, out = run([sys.executable, "-m", "unittest", "discover", "-s",
                           "tests", "-q"], main, timeout=TEST_TIMEOUT)
            if rc != 0:
                return rollback("tests failed (rc=%d)" % rc, out)
        rc, out = git(main, "push", "origin", MAIN_BRANCH)
        if rc != 0:
            return rollback("push failed", out)
        sha = new.strip()
        with store.lock():
            board = store.load()
            t = find_task(board, args.id)
            t["state"], t["merge_sha"] = "done", sha
            t["session"], t["claimed_at"] = None, None
            store.save(board)
            store.journal(session, "DONE", args.id,
                          "merged %s into main" % args.branch, basis=sha)
    print(sha)
    return 0


def fetch_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "artel-dev-board"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def cmd_ci(args, fetch=None, token_path=None):
    fetch = fetch or fetch_json
    path = Path(token_path or TOKEN_PATH)
    try:
        token = path.read_text().strip()
    except OSError as e:
        raise BoardError("cannot read token file %s: %s" % (path, e.strerror))
    if not token:
        raise BoardError("token file %s is empty" % path)
    url = ("https://api.github.com/repos/%s/actions/runs?branch=main"
           "&per_page=5" % GITHUB_REPO)
    try:
        data = fetch(url, token)
    except Exception as e:  # never include request headers in the message
        raise BoardError("GitHub request failed: %s" % type(e).__name__
                         + (" %s" % e.code if hasattr(e, "code") else ""))
    runs = data.get("workflow_runs", [])
    if args.sha:
        runs = [r for r in runs if r.get("head_sha", "").startswith(args.sha)]
    for r in runs:
        print("%s %s %s %s" % (r.get("head_sha", "")[:7], r.get("status"),
                               r.get("conclusion") or "-", r.get("html_url")))
    if not runs:
        print("no matching runs")
        return 2
    latest = runs[0]
    if latest.get("status") != "completed":
        return 2
    return 0 if latest.get("conclusion") == "success" else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="board.py", description=__doc__.split("\n")[0])
    p.add_argument("--repo", default=".", help="repository directory (default .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="parse the plan into a board")
    s.add_argument("--from", dest="plan", required=True, metavar="PLAN")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("claim", help="take the next claimable task")
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("release", help="give a task back")
    s.add_argument("id", type=int)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("fail", help="give a task back as failed")
    s.add_argument("id", type=int)
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_fail)

    s = sub.add_parser("done", help="mark a task done without a merge "
                                    "(controller tasks)")
    s.add_argument("id", type=int)
    s.add_argument("--sha")
    s.add_argument("--note")
    s.add_argument("--session")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("note", help="append a journal entry")
    s.add_argument("kind", choices=KINDS)
    s.add_argument("id", type=int)
    s.add_argument("text")
    s.add_argument("--basis")
    s.add_argument("--session")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("tail", help="journal entries since this session's cursor")
    s.add_argument("--session", required=True)
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_tail)

    s = sub.add_parser("status", help="task table and counts")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("integrate", help="merge a task branch into main, test, push")
    s.add_argument("id", type=int)
    s.add_argument("--branch", required=True)
    s.set_defaults(fn=cmd_integrate)

    s = sub.add_parser("ci", help="GitHub Actions result for main")
    s.add_argument("--sha")
    s.set_defaults(fn=cmd_ci)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except BoardError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ENV if args.cmd == "ci" else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

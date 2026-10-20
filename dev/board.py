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
import shutil
import signal
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
GIT_TIMEOUT = 120
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
KINDS = ("FACT", "FAIL", "NOTE", "SPEC-GAP")
GITHUB_REPO = "EugeneBerezov/artel"
TOKEN_PATH = Path.home() / ".config" / "artel-dev" / "github-token"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

EXIT_ERROR = 1
EXIT_NOTHING = 3
EXIT_ENV = 5  # ci: token missing / HTTP failure


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


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run(cmd, cwd, timeout=None):
    """Run a command in its own process group, stderr merged into stdout.

    Never reads stdin. On timeout or any interruption (Ctrl-C, SIGTERM) the
    whole group is killed. Returns (rc, output); rc 124 on timeout.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, _ = proc.communicate()
        return 124, (out or "") + "\n[timed out after %ss]" % timeout
    except BaseException:
        _kill_group(proc)
        raise


def git(cwd, *args, timeout=GIT_TIMEOUT):
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
            return json.loads(self.board_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BoardError("no board; run: board.py init --from docs/plan.md")

    def save(self, board):
        tmp = self.board_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
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
    if args.refresh:
        return _refresh(store, args.plan, tasks)
    with store.lock():
        if store.exists() and not args.force:
            raise BoardError("board already exists (use --force to replace "
                             "it: state, journal and cursors are moved to "
                             "a backup-<time> directory)")
        if store.exists():
            backup = store.dir / ("backup-" + now_iso().replace(":", ""))
            n = 1
            while backup.exists():
                n += 1
                backup = store.dir / ("backup-%s-%d" % (
                    now_iso().replace(":", ""), n))
            backup.mkdir()
            for name in ("board.json", "journal.md", "cursors"):
                if (store.dir / name).exists():
                    shutil.move(str(store.dir / name), str(backup / name))
            print("previous state backed up to %s" % backup)
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


def dep_ok(t):
    """A dependency counts once merged and not waiting on a cross-review."""
    return t["state"] == "done" and t.get("review") in (None, "pass")


def _review_for(tasks, session, review_only):
    """The cross-review this session holds, or the next one it may take.

    A session never reviews its own merge. A session holding a claimed task
    finishes it first (unless review_only).
    """
    pending = [t for t in tasks if t["state"] == "done"
               and t.get("review") == "pending"
               and t.get("author") != session]
    mine = [t for t in pending if t.get("reviewer") == session]
    if mine:
        return mine[0]
    if not review_only and any(t["state"] == "claimed"
                               and t["session"] == session for t in tasks):
        return None
    free = [t for t in pending if not t.get("reviewer")]
    return min(free, key=lambda t: (t["wave"], t["id"])) if free else None


def describe_review(board, t):
    lo, hi = t["plan_lines"]
    return "\n".join([
        "kind: review",
        "id: %d" % t["id"],
        "title: %s" % t["title"],
        "risky: %s" % ("yes" if t["risky"] else "no"),
        "author: %s" % t.get("author", "-"),
        "merge: %s" % t["merge_sha"],
        "diff: git diff %s^1 %s" % (t["merge_sha"], t["merge_sha"]),
        "plan: %s:%d-%d" % (board["source"], lo, hi),
        "verdict: board.py review %d --verdict pass|fail --reason R "
        "--session <you>" % t["id"],
    ])


def cmd_review(args):
    session = check_session(args.session)
    store = Store(args.repo)
    with store.lock():
        board = store.load()
        t = find_task(board, args.id)
        if t["state"] != "done" or t.get("review") != "pending":
            raise BoardError("T%d has no pending review" % args.id)
        if t.get("reviewer") != session:
            raise BoardError("T%d review is held by %s, not %s" % (
                args.id, t.get("reviewer") or "nobody", session))
        if args.verdict == "pass":
            t["review"] = "pass"
            store.journal(session, "REVIEW-PASS", args.id, args.reason,
                          basis=t["merge_sha"])
        else:
            t["state"], t["merge_sha"], t["review"] = "open", None, None
            t["reviewer"] = None
            t["fails"] += 1
            text = args.reason
            if t["fails"] >= MAX_FAILS + 1:
                t["state"] = "stuck"
                text += " (stuck after %d fails)" % t["fails"]
            store.journal(session, "REVIEW-FAIL", args.id, text)
        store.save(board)
    print("T%d review %s" % (args.id, args.verdict))


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
        review = _review_for(tasks, session, args.review_only)
        if review is not None:
            if review.get("reviewer") != session:
                review["reviewer"] = session
                store.save(board)
                store.journal(session, "REVIEW", review["id"],
                              "cross-review of %s's merge"
                              % review.get("author", "-"),
                              basis=review["merge_sha"])
            print(describe_review(board, review))
            return 0
        if args.review_only:
            print("no review pending for %s" % session, file=sys.stderr)
            return EXIT_NOTHING
        cands = [t for t in tasks if t["state"] == "open"
                 and not t["controller"]
                 and all(dep_ok(by_id[d]) for d in t["deps"])]
        if not cands:
            print(_nothing_claimable(tasks, by_id), file=sys.stderr)
            return EXIT_NOTHING
        t = min(cands, key=lambda x: (x["wave"], x["id"]))
        t["state"], t["session"], t["claimed_at"] = "claimed", session, now_iso()
        store.save(board)
        store.journal(session, "CLAIM", t["id"], t["title"])
        print(describe(board, t, session))
    return 0


PLAN_FIELDS = ("title", "wave", "deps", "risky", "controller", "plan_lines")


def _refresh(store, plan, parsed):
    """Re-read the plan into an existing board, keeping every task's state.

    Titles, waves, deps, flags and line ranges follow the plan; new tasks
    start open; a task gone from the plan is dropped only if it is open.
    """
    with store.lock():
        board = store.load()
        old = {t["id"]: t for t in board["tasks"]}
        new_ids = {t["id"] for t in parsed}
        gone = sorted(i for i in old if i not in new_ids)
        busy = [i for i in gone if old[i]["state"] != "open"]
        if busy:
            raise BoardError("tasks %s are not open but missing from the "
                             "plan" % ["T%d" % i for i in busy])
        tasks, added = [], []
        for t in parsed:
            if t["id"] in old:
                kept = old[t["id"]]
                kept.update({k: t[k] for k in PLAN_FIELDS})
                tasks.append(kept)
            else:
                tasks.append(t)
                added.append(t["id"])
        board["tasks"], board["source"] = tasks, plan
        store.save(board)
        text = "board refreshed from %s: %d tasks" % (plan, len(tasks))
        if added:
            text += ", added %s" % " ".join("T%d" % i for i in added)
        if gone:
            text += ", removed %s" % " ".join("T%d" % i for i in gone)
        store.journal("-", "PLAN", "-", text)
    print(text)


def cmd_reopen(args):
    """done/stuck -> open: a merged task needs more work (red CI, review)."""
    store = Store(args.repo)
    with store.lock():
        board = store.load()
        t = find_task(board, args.id)
        if t["state"] not in ("done", "stuck"):
            raise BoardError("T%d is %s; reopen takes done or stuck tasks"
                             % (args.id, t["state"]))
        who = check_session(args.session) if args.session else "-"
        if t["state"] == "stuck":
            t["fails"] = 0
        t["state"], t["merge_sha"] = "open", None
        t["session"], t["claimed_at"] = None, None
        t["review"], t["reviewer"] = None, None
        store.save(board)
        store.journal(who, "REOPEN", args.id, args.reason)
        held = [d for d in board["tasks"] if args.id in d["deps"]
                and d["state"] in ("claimed", "done")]
    print("T%d -> open" % args.id)
    if held:
        print("warning: dependents already %s" % ", ".join(
            "T%d %s" % (d["id"], d["state"]) for d in held), file=sys.stderr)


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
            elif dep["state"] == "done" and not dep_ok(dep):
                held.setdefault(dep["id"], "review by %s" % (
                    dep.get("reviewer") or "the other session"))
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
    pending = [t for t in tasks if t["state"] == "done"
               and t.get("review") == "pending"]
    if pending:
        parts.append("pending cross-reviews: %s (a session never reviews its "
                     "own merge; the controller can take one with: claim "
                     "--session main --review-only)" % ", ".join(
                         "T%d by %s, reviewer %s" % (
                             t["id"], t.get("author", "-"),
                             t.get("reviewer") or "-") for t in pending))
    stuck = [t["id"] for t in tasks if t["state"] == "stuck"]
    if stuck:
        parts.append("stuck: %s" % ", ".join("T%d" % i for i in stuck))
    if not open_ and not held and not pending:
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
    store = Store(args.repo)
    with store.lock():
        t = find_task(store.load(), args.id)
        held_review = t["state"] == "done" and t.get("review") == "pending"
    if held_review:
        return _release_review(store, args.id, args.reason)
    state = _reopen(store, args.id, "RELEASE",
                    args.reason or "released", None, False)
    print("T%d -> %s" % (args.id, state))


def _release_review(store, tid, reason):
    """A done task's pending cross-review goes back to the pool."""
    with store.lock():
        board = store.load()
        t = find_task(board, tid)
        who = t.get("reviewer") or "-"
        t["reviewer"] = None
        store.save(board)
        store.journal(who, "RELEASE", tid,
                      "review released: %s" % (reason or "released"))
    print("T%d review -> unheld" % tid)


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
        who = t["session"] or "-"
        if args.session:
            who = check_session(args.session)
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
                start = int(cursor.read_text(encoding="utf-8").strip() or 0)
            except (FileNotFoundError, ValueError):
                start = 0
            if start > len(data):
                start = 0
        sys.stdout.write(data[start:].decode("utf-8", errors="replace"))
        if not args.all:
            cursor.write_text(str(len(data)), encoding="utf-8")


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
        state = t["state"]
        if state == "done" and t.get("review") == "pending":
            state = "review(%s)" % (t.get("reviewer") or "-")
        rows.append(("T%d" % t["id"], "W%d" % t["wave"], title, state,
                     t["session"] or "-", flags.strip() or "-"))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    counts = {}
    for t in board["tasks"]:
        key = t["state"]
        if key == "done" and t.get("review") == "pending":
            key = "review"
        counts[key] = counts.get(key, 0) + 1
    print()
    print("%s (%d total)" % (", ".join(
        "%s %d" % (s, counts.get(s, 0))
        for s in ("open", "claimed", "review", "done", "stuck")),
        len(board["tasks"])))


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


def _sync_problem(main):
    """Fetch origin; return a refusal message if local main != origin/main."""
    rc, out = git(main, "fetch", "origin")
    if rc != 0:
        return "git fetch origin failed:\n" + _tail_lines(out, 10)
    rc, local = git(main, "rev-parse", "HEAD")
    rc2, remote = git(main, "rev-parse", "--verify", "-q",
                      "refs/remotes/origin/" + MAIN_BRANCH)
    if rc != 0 or rc2 != 0:
        return "cannot resolve origin/%s" % MAIN_BRANCH
    if local.strip() == remote.strip():
        return None
    rc, out = git(main, "rev-list", "--left-right", "--count",
                  "HEAD...origin/" + MAIN_BRANCH)
    ahead, behind = (out.split() + ["?", "?"])[:2]
    if ahead == "0":
        fix = "run: cd %s && git pull --ff-only origin %s" % (main, MAIN_BRANCH)
        kind = "behind"
    elif behind == "0":
        fix = "run: cd %s && git push origin %s" % (main, MAIN_BRANCH)
        kind = "ahead of"
    else:
        fix = ("reconcile by hand in %s (git log --oneline --graph HEAD "
               "origin/%s)" % (main, MAIN_BRANCH))
        kind = "diverged from"
    return ("main is %s origin/%s (ahead %s, behind %s); %s"
            % (kind, MAIN_BRANCH, ahead, behind, fix))


def _remote_has(main, sha):
    rc, _ = git(main, "fetch", "origin")
    if rc != 0:
        return False
    rc, _ = git(main, "merge-base", "--is-ancestor", sha,
                "refs/remotes/origin/" + MAIN_BRANCH)
    return rc == 0


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def cmd_integrate(args):
    session = check_session(args.session)
    store = Store(args.repo)
    # Serialises integrations without blocking claim/note/tail for up to
    # TEST_TIMEOUT: the board lock is only taken for the bookkeeping.
    with store.lock("integrate.lock"):
        with store.lock():
            task = find_task(store.load(), args.id)
            if task["state"] == "done":
                raise BoardError("T%d is already done" % args.id)
            if task["state"] != "claimed" or task["session"] != session:
                raise BoardError("T%d is not claimed by %s (state %s, holder "
                                 "%s)" % (args.id, session, task["state"],
                                          task["session"] or "-"))
            title = task["title"]
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
        problem = _sync_problem(main)
        if problem:
            raise BoardError(problem)
        rc, head = git(main, "rev-parse", "HEAD")
        head = head.strip()

        old_term = signal.signal(signal.SIGTERM, _terminate)
        try:
            sha = _merge_test_push(main, args, title, head)
        finally:
            signal.signal(signal.SIGTERM, old_term)
        if sha is None:
            return EXIT_ERROR
        with store.lock():
            board = store.load()
            t = find_task(board, args.id)
            if t["state"] != "claimed" or t["session"] != session:
                print("warning: T%d changed hands while integrating (now %s, "
                      "holder %s); marking it done anyway, the merge is "
                      "pushed" % (args.id, t["state"], t["session"] or "-"),
                      file=sys.stderr)
            t["state"], t["merge_sha"] = "done", sha
            t["session"], t["claimed_at"] = None, None
            t["author"], t["review"], t["reviewer"] = session, "pending", None
            store.save(board)
            store.journal(session, "DONE", args.id,
                          "merged %s into main" % args.branch, basis=sha)
    print(sha)
    return 0


def _merge_test_push(main, args, title, head):
    """Merge, test and push. Returns the merge sha, or None after a rollback.

    Any exception (Ctrl-C, SIGTERM, bug) rolls main back to `head` before it
    propagates, unless the push already reached origin.
    """
    def rollback(why, output):
        git(main, "merge", "--abort")
        git(main, "reset", "--hard", head)
        print("integrate T%d failed: %s\n%s" % (
            args.id, why, _tail_lines(output)), file=sys.stderr)

    new = None
    pushed = False
    try:
        rc, out = git(main, "merge", "--no-ff", args.branch, "-m",
                      "Merge T%d: %s" % (args.id, title), "-m", TRAILER)
        if rc != 0:
            rollback("merge conflict" if "onflict" in out else "merge failed",
                     out)
            return None
        rc, new = git(main, "rev-parse", "HEAD")
        new = new.strip()
        if new == head:
            rollback("nothing to merge (branch already in main)", out)
            return None
        if (Path(main) / "tests").is_dir():
            rc, out = run([sys.executable, "-m", "unittest", "discover", "-s",
                           "tests", "-q"], main, timeout=TEST_TIMEOUT)
            if rc != 0:
                rollback("tests failed (rc=%d)" % rc, out)
                return None
        rc, out = git(main, "push", "origin", MAIN_BRANCH)
        if rc != 0:
            if "rejected" in out and re.search(
                    r"fetch first|non-fast-forward|stale info", out):
                why = ("push rejected: origin/%s moved while testing; in %s "
                       "run: git pull --ff-only origin %s, then integrate "
                       "again" % (MAIN_BRANCH, main, MAIN_BRANCH))
            else:
                why = "push failed"
            rollback(why, out)
            return None
        pushed = True
        return new
    except BaseException:
        if not pushed:
            if new and _remote_has(main, new):
                print("warning: interrupted during push, but the merge %s is "
                      "on origin/%s; main left as is" % (new[:7], MAIN_BRANCH),
                      file=sys.stderr)
            else:
                git(main, "merge", "--abort")
                git(main, "reset", "--hard", head)
                print("interrupted: main rolled back to %s" % head[:7],
                      file=sys.stderr)
        raise


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
        token = path.read_text(encoding="utf-8").strip()
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
    elif runs:
        runs = [r for r in runs if r.get("head_sha") == runs[0].get("head_sha")]
    for r in runs:
        print("%s %s %s %s" % (r.get("head_sha", "")[:7], r.get("status"),
                               r.get("conclusion") or "-", r.get("html_url")))
    if not runs:
        print("no run yet")
        return 2
    pending = [r for r in runs if r.get("status") != "completed"]
    failed = [r for r in runs if r.get("status") == "completed"
              and r.get("conclusion") != "success"]
    if failed:
        return 1
    if pending:
        print("pending")
        return 4
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="board.py", description=__doc__.split("\n")[0])
    p.add_argument("--repo", default=".", help="repository directory (default .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="parse the plan into a board")
    s.add_argument("--from", dest="plan", required=True, metavar="PLAN")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--force", action="store_true")
    g.add_argument("--refresh", action="store_true",
                   help="re-read the plan, keep task states")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("reopen", help="done or stuck task back to open")
    s.add_argument("id", type=int)
    s.add_argument("--reason", required=True)
    s.add_argument("--session")
    s.set_defaults(fn=cmd_reopen)

    s = sub.add_parser("claim", help="take the next cross-review or task")
    s.add_argument("--session", required=True)
    s.add_argument("--review-only", action="store_true",
                   help="only take a cross-review (controller)")
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("review", help="verdict on a held cross-review")
    s.add_argument("id", type=int)
    s.add_argument("--verdict", choices=("pass", "fail"), required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_review)

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
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_integrate)

    s = sub.add_parser("ci", help="GitHub Actions result for main")
    s.add_argument("--sha")
    s.set_defaults(fn=cmd_ci)
    return p


def _argv_text(value):
    """Undo surrogateescape from a non-UTF-8 locale: argv bytes -> UTF-8 text."""
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return os.fsencode(value).decode("utf-8", errors="replace")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    for key, value in vars(args).items():
        if isinstance(value, str):
            setattr(args, key, _argv_text(value))
    try:
        return args.fn(args) or 0
    except BoardError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ENV if args.cmd == "ci" else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

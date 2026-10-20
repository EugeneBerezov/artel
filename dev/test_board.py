"""Tests for dev/board.py. Run: python -m unittest discover -s dev -q"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402

BOARD_PY = str(Path(board.__file__).resolve())

FAKE_PLAN = """# Tiny plan

## Waves

| Wave | Tasks (RISKY in bold) |
|---|---|
| W1 | 1 First |
| W2 | **2 Second** · 3 Third |
| W3 | 4 Fourth → 5 Fifth |

---

### Task 1: First thing

**Files:**
- Create: `a.py`

```python
## not a heading, inside a fence
### Task 99: not a task either
```

---

### Task 2: Second thing — **RISKY (strong-model review)**

body two

### Task 3: Third thing

body three

### Task 4: Fourth thing

body four

### Task 5: Fifth thing — controller task, owner consent required

body five

## Review routing

not part of task 5
"""

# a plan whose waves are all single tasks, for claim-order tests
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git", *args], cwd=cwd, env=env, text=True,
                       capture_output=True)
    if r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stdout}{r.stderr}")
    return r.stdout.strip()


def make_repo(tmp, plan=FAKE_PLAN):
    """A clone ('work') of a bare remote, main branch, plan committed."""
    remote = Path(tmp) / "remote.git"
    work = Path(tmp) / "work"
    git(tmp, "init", "--bare", "-b", "main", str(remote))
    git(tmp, "init", "-b", "main", str(work))
    git(work, "remote", "add", "origin", str(remote))
    (work / "docs").mkdir()
    (work / "docs" / "plan.md").write_text(plan)
    (work / "base.txt").write_text("base\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    return work, remote


def cli(repo, *args, env=None, expect=None):
    e = dict(os.environ, **GIT_ENV)
    e.pop("ARTEL_DEV_SESSION", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, BOARD_PY, "--repo", str(repo), *args],
                       text=True, encoding="utf-8", capture_output=True, env=e)
    if expect is not None and r.returncode != expect:
        raise AssertionError(
            f"{args}: rc={r.returncode} want {expect}\n{r.stdout}\n{r.stderr}")
    return r


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.repo, self.remote = make_repo(self.tmp)

    def init(self):
        return cli(self.repo, "init", "--from", "docs/plan.md", expect=0)

    def store(self):
        d = git(self.repo, "rev-parse", "--path-format=absolute",
                "--git-common-dir")
        return Path(d) / "artel-dev"

    def tasks(self):
        data = json.loads((self.store() / "board.json").read_text())
        return {t["id"]: t for t in data["tasks"]}


class ParsePlan(unittest.TestCase):
    def setUp(self):
        self.tasks = {t["id"]: t for t in board.parse_plan(FAKE_PLAN)}

    def test_finds_exactly_the_real_tasks(self):
        self.assertEqual(sorted(self.tasks), [1, 2, 3, 4, 5])

    def test_title_wave_risky(self):
        self.assertEqual(self.tasks[2]["title"], "Second thing")
        self.assertTrue(self.tasks[2]["risky"])
        self.assertFalse(self.tasks[1]["risky"])
        self.assertEqual([self.tasks[i]["wave"] for i in range(1, 6)],
                         [1, 2, 2, 3, 3])

    def test_risky_also_from_bold_in_wave_table(self):
        plan = FAKE_PLAN.replace("| W3 | 4 Fourth", "| W3 | **4 Fourth**")
        t = {x["id"]: x for x in board.parse_plan(plan)}
        self.assertTrue(t[4]["risky"])
        self.assertFalse(t[3]["risky"])

    def test_deps_are_previous_wave_plus_arrow_chain(self):
        deps = {i: self.tasks[i]["deps"] for i in self.tasks}
        self.assertEqual(deps, {1: [], 2: [1], 3: [1], 4: [2, 3],
                                5: [2, 3, 4]})

    def test_controller_flag(self):
        self.assertTrue(self.tasks[5]["controller"])
        self.assertFalse(self.tasks[4]["controller"])

    def test_line_range_ignores_fenced_headings_and_stops_at_h2(self):
        lines = FAKE_PLAN.splitlines()
        a, b = self.tasks[1]["plan_lines"]
        self.assertTrue(lines[a - 1].startswith("### Task 1:"))
        self.assertTrue(lines[b - 1].startswith("```") or
                        "Task 99" in lines[b - 2])
        a5, b5 = self.tasks[5]["plan_lines"]
        self.assertEqual(lines[b5 - 1], "body five")

    def test_explicit_depends_line_overrides_default(self):
        plan = FAKE_PLAN.replace("body three",
                                 "**Depends on:** Task 1, Task 2\n\nbody three")
        t = {x["id"]: x for x in board.parse_plan(plan)}
        self.assertEqual(t[3]["deps"], [1, 2])

    def test_table_and_headings_must_agree(self):
        with self.assertRaises(board.BoardError):
            board.parse_plan(FAKE_PLAN.replace("| W1 | 1 First |",
                                               "| W1 | 1 First · 9 Ghost |"))


class InitStatus(TmpCase):
    def test_init_refuses_second_time_unless_force(self):
        self.init()
        cli(self.repo, "init", "--from", "docs/plan.md", expect=1)
        cli(self.repo, "init", "--from", "docs/plan.md", "--force", expect=0)

    def test_status_table_and_counts(self):
        self.init()
        out = cli(self.repo, "status", expect=0).stdout
        self.assertIn("Second thing", out)
        self.assertIn("open 5", out)
        self.assertIn("5 total", out)

    def test_storage_is_in_git_common_dir_shared_by_worktrees(self):
        self.init()
        wt = Path(self.tmp) / "wt"
        git(self.repo, "worktree", "add", "-b", "other", str(wt))
        out = cli(wt, "status", expect=0).stdout
        self.assertIn("First thing", out)


class Claim(TmpCase):
    def claim(self, s):
        return cli(self.repo, "claim", "--session", s)

    def test_lowest_wave_then_id_and_deps(self):
        self.init()
        r = self.claim("A")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("id: 1", r.stdout)
        self.assertIn("branch: dev/T1", r.stdout)
        self.assertIn(".claude/worktrees/A-T1", r.stdout)
        self.assertIn("risky: no", r.stdout)
        self.assertIn("docs/plan.md:", r.stdout)
        # task 1 held: nothing else claimable (deps), exit 3 with explanation
        r = self.claim("B")
        self.assertEqual(r.returncode, 3)
        self.assertIn("waiting", r.stderr + r.stdout)
        self.assertIn("A", r.stderr + r.stdout)

    def test_second_claim_by_same_session_reprints_same_task(self):
        self.init()
        a = self.claim("A").stdout
        self.assertEqual(self.claim("A").stdout, a)
        self.assertEqual(self.tasks()[1]["session"], "A")

    def test_next_wave_after_done_and_risky_flag(self):
        self.init()
        self.claim("A")
        cli(self.repo, "done", "1", expect=0)
        r = self.claim("A")
        self.assertIn("id: 2", r.stdout)
        self.assertIn("risky: yes", r.stdout)
        r = self.claim("B")
        self.assertIn("id: 3", r.stdout)

    def test_controller_tasks_are_never_claimed(self):
        self.init()
        for i in (1, 2, 3, 4):
            self.claim("A")
            cli(self.repo, "done", str(i), expect=0)
        r = self.claim("A")
        self.assertEqual(r.returncode, 3)
        self.assertIn("controller", r.stdout + r.stderr)

    def test_concurrent_claims_exactly_one_wins(self):
        self.init()
        procs = [subprocess.Popen(
            [sys.executable, BOARD_PY, "--repo", str(self.repo), "claim",
             "--session", s], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(os.environ, **GIT_ENV)) for s in "ABCDEF"]
        codes = sorted(p.wait() for p in procs)
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(codes, [0, 3, 3, 3, 3, 3])
        held = [t for t in self.tasks().values() if t["state"] == "claimed"]
        self.assertEqual(len(held), 1)


class ReleaseFail(TmpCase):
    def test_release_reopens_and_journals(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "release", "1", "--reason", "switching", expect=0)
        self.assertEqual(self.tasks()[1]["state"], "open")
        self.assertIsNone(self.tasks()[1]["session"])
        self.assertIn("RELEASE T1: switching",
                      (self.store() / "journal.md").read_text())

    def test_two_fails_make_stuck(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "fail", "1", "--reason", "tests red", expect=0)
        self.assertEqual(self.tasks()[1]["state"], "open")
        self.assertEqual(self.tasks()[1]["fails"], 1)
        cli(self.repo, "claim", "--session", "B", expect=0)
        cli(self.repo, "fail", "1", "--reason", "again", expect=0)
        self.assertEqual(self.tasks()[1]["state"], "stuck")
        r = cli(self.repo, "claim", "--session", "A")
        self.assertEqual(r.returncode, 3)
        j = (self.store() / "journal.md").read_text()
        self.assertIn("B FAIL T1: again", j)
        self.assertIn("A FAIL T1: tests red", j)


class ReopenRefresh(TmpCase):
    def test_reopen_done_task_blocks_dependents_again(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "done", "1", "--sha", "abc", expect=0)
        cli(self.repo, "reopen", "1", "--reason", "CI red on win",
            "--session", "main", expect=0)
        t = self.tasks()[1]
        self.assertEqual((t["state"], t["merge_sha"]), ("open", None))
        r = cli(self.repo, "claim", "--session", "B", expect=0)
        self.assertIn("id: 1", r.stdout)
        self.assertIn("main REOPEN T1: CI red on win",
                      (self.store() / "journal.md").read_text())

    def test_reopen_refuses_open_and_claimed(self):
        self.init()
        cli(self.repo, "reopen", "1", "--reason", "x", expect=1)
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "reopen", "1", "--reason", "x", expect=1)

    def test_reopen_stuck_resets_fails(self):
        self.init()
        for s in ("A", "B"):
            cli(self.repo, "claim", "--session", s, expect=0)
            cli(self.repo, "fail", "1", "--reason", "red", expect=0)
        self.assertEqual(self.tasks()[1]["state"], "stuck")
        cli(self.repo, "reopen", "1", "--reason", "owner fixed plan", expect=0)
        self.assertEqual(self.tasks()[1]["fails"], 0)

    def test_reopen_warns_about_claimed_dependents(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "done", "1", expect=0)
        cli(self.repo, "claim", "--session", "A", expect=0)  # T2
        r = cli(self.repo, "reopen", "1", "--reason", "x", expect=0)
        self.assertIn("T2 claimed", r.stderr)

    def test_refresh_keeps_states_and_adds_tasks(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "done", "1", "--sha", "abc", expect=0)
        cli(self.repo, "claim", "--session", "A", expect=0)  # T2
        plan = FAKE_PLAN.replace("| W2 | **2 Second** · 3 Third |",
                                 "| W2 | **2 Second** · 3 Third · 6 Fix |")
        plan = plan.replace("## Review routing",
                            "### Task 6: Fix of one\n\nbody six\n\n"
                            "## Review routing")
        plan = "intro line\n" + plan
        (self.repo / "docs" / "plan.md").write_text(plan)
        r = cli(self.repo, "init", "--from", "docs/plan.md", "--refresh",
                expect=0)
        self.assertIn("added T6", r.stdout)
        t = self.tasks()
        self.assertEqual((t[1]["state"], t[1]["merge_sha"]), ("done", "abc"))
        self.assertEqual((t[2]["state"], t[2]["session"]), ("claimed", "A"))
        self.assertEqual(t[6]["state"], "open")
        self.assertIn(6, t[4]["deps"])
        old = board.parse_plan(FAKE_PLAN)[0]["plan_lines"]
        self.assertEqual(t[1]["plan_lines"], [old[0] + 1, old[1] + 1])
        self.assertIn("PLAN T-: board refreshed",
                      (self.store() / "journal.md").read_text())

    def test_refresh_refuses_to_drop_a_non_open_task(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        plan = FAKE_PLAN.replace("| W1 | 1 First |\n", "")
        plan = plan.replace("deps", "deps")
        self.assertIn("### Task 1:", plan)
        start = plan.index("### Task 1:")
        plan = plan[:start] + plan[plan.index("### Task 2:"):]
        (self.repo / "docs" / "plan.md").write_text(plan)
        r = cli(self.repo, "init", "--from", "docs/plan.md", "--refresh",
                expect=1)
        self.assertIn("T1", r.stderr)
        self.assertEqual(self.tasks()[1]["state"], "claimed")

    def test_force_and_refresh_are_exclusive(self):
        self.init()
        cli(self.repo, "init", "--from", "docs/plan.md", "--force",
            "--refresh", expect=2)


class CrossReview(TmpCase):
    def merged_by(self, tid, session):
        """Simulate integrate's bookkeeping for a merge by `session`."""
        path = self.store() / "board.json"
        data = json.loads(path.read_text())
        for t in data["tasks"]:
            if t["id"] == tid:
                t.update(state="done", merge_sha="m%d" % tid, session=None,
                         author=session, review="pending", reviewer=None)
        path.write_text(json.dumps(data))

    def test_dependents_wait_for_cross_review_by_the_other_session(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        r = cli(self.repo, "claim", "--session", "A", expect=3)
        self.assertIn("review by", r.stderr)
        r = cli(self.repo, "claim", "--session", "B", expect=0)
        self.assertIn("kind: review", r.stdout)
        self.assertIn("id: 1", r.stdout)
        self.assertIn("merge: m1", r.stdout)
        r = cli(self.repo, "claim", "--session", "B", expect=0)
        self.assertIn("kind: review", r.stdout)  # held review is reprinted
        cli(self.repo, "review", "1", "--verdict", "pass", "--reason", "ok",
            "--session", "A", expect=1)  # not the holder
        cli(self.repo, "review", "1", "--verdict", "pass", "--reason", "ok",
            "--session", "B", expect=0)
        self.assertEqual(self.tasks()[1]["review"], "pass")
        r = cli(self.repo, "claim", "--session", "A", expect=0)
        self.assertIn("id: 2", r.stdout)
        j = (self.store() / "journal.md").read_text()
        self.assertIn("B REVIEW T1", j)
        self.assertIn("B REVIEW-PASS T1: ok", j)

    def test_fail_verdict_reopens_task(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        cli(self.repo, "claim", "--session", "B", expect=0)
        cli(self.repo, "review", "1", "--verdict", "fail", "--reason",
            "lock leak", "--session", "B", expect=0)
        t = self.tasks()[1]
        self.assertEqual((t["state"], t["merge_sha"], t["review"]),
                         ("open", None, None))
        self.assertIn("B REVIEW-FAIL T1: lock leak",
                      (self.store() / "journal.md").read_text())

    def test_session_with_a_claimed_task_finishes_it_first(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        cli(self.repo, "claim", "--session", "B", expect=0)  # review T1
        cli(self.repo, "review", "1", "--verdict", "pass", "--reason", "ok",
            "--session", "B", expect=0)
        cli(self.repo, "claim", "--session", "A", expect=0)  # T2
        cli(self.repo, "claim", "--session", "B", expect=0)  # T3
        self.merged_by(2, "A")
        r = cli(self.repo, "claim", "--session", "B", expect=0)
        self.assertIn("id: 3", r.stdout)
        self.assertNotIn("kind: review", r.stdout)
        r = cli(self.repo, "claim", "--session", "main", "--review-only",
                expect=0)
        self.assertIn("kind: review", r.stdout)
        self.assertIn("id: 2", r.stdout)

    def test_review_only_with_nothing_pending(self):
        self.init()
        cli(self.repo, "claim", "--session", "main", "--review-only",
            expect=3)

    def test_old_done_tasks_without_review_field_satisfy_deps(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "done", "1", expect=0)
        r = cli(self.repo, "claim", "--session", "A", expect=0)
        self.assertIn("id: 2", r.stdout)

    def test_status_shows_review_state(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        self.assertIn("review(-)", cli(self.repo, "status", expect=0).stdout)

    def test_release_frees_a_review_held_by_a_dead_session(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        cli(self.repo, "claim", "--session", "B", expect=0)
        cli(self.repo, "release", "1", "--reason", "B died", expect=0)
        t = self.tasks()[1]
        self.assertEqual((t["state"], t["review"], t["reviewer"]),
                         ("done", "pending", None))
        r = cli(self.repo, "claim", "--session", "main", "--review-only",
                expect=0)
        self.assertIn("id: 1", r.stdout)

    def test_nothing_claimable_names_pending_reviews(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        r = cli(self.repo, "claim", "--session", "A", expect=3)
        self.assertIn("pending cross-reviews: T1 by A", r.stderr)
        self.assertIn("--review-only", r.stderr)
        self.assertIn("review 1", cli(self.repo, "status", expect=0).stdout)

    def test_repeated_review_fails_make_stuck(self):
        self.init()
        for n in range(3):
            cli(self.repo, "claim", "--session", "A", expect=0)
            self.merged_by(1, "A")
            cli(self.repo, "claim", "--session", "B", expect=0)
            cli(self.repo, "review", "1", "--verdict", "fail", "--reason",
                "bad %d" % n, "--session", "B", expect=0)
        self.assertEqual(self.tasks()[1]["state"], "stuck")

    def test_reopen_clears_pending_review(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        self.merged_by(1, "A")
        cli(self.repo, "reopen", "1", "--reason", "red", expect=0)
        self.assertIsNone(self.tasks()[1]["review"])


class Notes(TmpCase):
    def test_note_format_and_limits(self):
        self.init()
        cli(self.repo, "note", "FACT", "1", "hello", "--basis", "plan.md:5",
            "--session", "A", expect=0)
        line = (self.store() / "journal.md").read_text().strip().splitlines()[-1]
        self.assertRegex(line, r"^- \d{4}-\d\d-\d\dT[\d:]+Z?\S* A FACT T1: "
                               r"hello \[basis: plan\.md:5\]$")
        cli(self.repo, "note", "NOTE", "1", "x" * 400, expect=0)
        cli(self.repo, "note", "NOTE", "1", "x" * 401, expect=1)
        # bytes, not characters: 201 two-byte chars = 402 bytes
        cli(self.repo, "note", "NOTE", "1", "é" * 201, expect=1)
        cli(self.repo, "note", "NOTE", "1", "é" * 200, expect=0)
        cli(self.repo, "note", "NOTE", "1", "ok", "--basis", "b" * 201,
            expect=1)
        cli(self.repo, "note", "BOGUS", "1", "ok", expect=2)
        cli(self.repo, "note", "NOTE", "99", "ok", expect=1)

    def test_newlines_are_collapsed(self):
        self.init()
        cli(self.repo, "note", "SPEC-GAP", "2", "a\nb\n- fake entry",
            "--session", "A", expect=0)
        lines = (self.store() / "journal.md").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_tail_cursor(self):
        self.init()
        cli(self.repo, "note", "FACT", "1", "one", "--session", "A", expect=0)
        r = cli(self.repo, "tail", "--session", "B", expect=0)
        self.assertIn("one", r.stdout)
        r = cli(self.repo, "tail", "--session", "B", expect=0)
        self.assertEqual(r.stdout.strip(), "")
        cli(self.repo, "note", "FACT", "1", "two", "--session", "A", expect=0)
        r = cli(self.repo, "tail", "--session", "B", expect=0)
        self.assertIn("two", r.stdout)
        self.assertNotIn("one", r.stdout)
        r = cli(self.repo, "tail", "--session", "B", "--all", expect=0)
        self.assertIn("one", r.stdout)
        self.assertIn("two", r.stdout)
        # --all does not move the cursor backwards or forwards
        r = cli(self.repo, "tail", "--session", "B", expect=0)
        self.assertEqual(r.stdout.strip(), "")
        # another session still sees everything
        r = cli(self.repo, "tail", "--session", "C", expect=0)
        self.assertIn("one", r.stdout)


class Integrate(TmpCase):
    def branch_with(self, name, fname, content, base="main"):
        git(self.repo, "checkout", "-b", name, base)
        (self.repo / fname).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", f"work on {fname}")
        git(self.repo, "checkout", "main")

    def start(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)

    def test_success_merges_pushes_and_marks_done(self):
        self.start()
        self.branch_with("dev/T1", "one.txt", "1\n")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sha = r.stdout.strip().splitlines()[-1]
        self.assertEqual(sha, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(git(self.remote, "rev-parse", "main"), sha)
        msg = git(self.repo, "log", "-1", "--format=%B")
        self.assertIn("Merge T1: First thing", msg)
        self.assertTrue(msg.strip().endswith(
            "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"))
        self.assertEqual(len(git(self.repo, "log", "-1",
                                 "--format=%P").split()), 2)  # --no-ff
        t = self.tasks()[1]
        self.assertEqual((t["state"], t["merge_sha"]), ("done", sha))
        self.assertIn("DONE T1", (self.store() / "journal.md").read_text())
        self.assertEqual((t["author"], t["review"]), ("A", "pending"))

    def test_conflict_rolls_back(self):
        self.start()
        self.branch_with("dev/T1", "base.txt", "branch side\n")
        (self.repo / "base.txt").write_text("main side\n")
        git(self.repo, "commit", "-am", "main edit")
        git(self.repo, "push", "origin", "main")
        head = git(self.repo, "rev-parse", "HEAD")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("onflict", r.stdout + r.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain",
                             "--untracked-files=no"), "")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(self.tasks()[1]["state"], "claimed")
        self.assertEqual(git(self.remote, "rev-parse", "main"), head)

    def test_red_tests_roll_back(self):
        self.start()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_x(self):\n        self.assertFalse(__import__('os')"
            ".path.exists('one.txt'))\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "tests")
        git(self.repo, "push", "origin", "main")
        head = git(self.repo, "rev-parse", "HEAD")
        self.branch_with("dev/T1", "one.txt", "1\n")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL", r.stdout + r.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.repo / "one.txt").exists())
        self.assertEqual(git(self.remote, "rev-parse", "main"), head)
        self.assertEqual(self.tasks()[1]["state"], "claimed")

    def test_green_tests_allow_merge(self):
        self.start()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "tests")
        git(self.repo, "push", "origin", "main")
        self.branch_with("dev/T1", "one.txt", "1\n")
        cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A", expect=0)

    def test_refuses_dirty_main_and_wrong_branch(self):
        self.start()
        self.branch_with("dev/T1", "one.txt", "1\n")
        (self.repo / "base.txt").write_text("dirty\n")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("clean", r.stdout + r.stderr)
        git(self.repo, "checkout", "--", "base.txt")
        git(self.repo, "checkout", "dev/T1")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("main", r.stdout + r.stderr)

    def test_runs_from_a_linked_worktree_but_merges_in_main(self):
        self.start()
        self.branch_with("dev/T1", "one.txt", "1\n")
        wt = Path(self.tmp) / "wt"
        git(self.repo, "worktree", "add", "-b", "scratch", str(wt))
        cli(wt, "integrate", "1", "--branch", "dev/T1", "--session", "A", expect=0)
        self.assertTrue((self.repo / "one.txt").exists())

    def test_push_failure_rolls_back(self):
        self.start()
        self.branch_with("dev/T1", "one.txt", "1\n")
        # make the remote reject pushes
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        head = git(self.repo, "rev-parse", "HEAD")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1", "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.tasks()[1]["state"], "claimed")


class IntegrateSafety(TmpCase):
    def start(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        git(self.repo, "checkout", "-b", "dev/T1")
        (self.repo / "one.txt").write_text("1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "work")
        git(self.repo, "checkout", "main")

    def add_slow_test(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_slow.py").write_text(
            "import os, subprocess, time, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        p = subprocess.Popen(['sleep', '300'])\n"
            "        open('child.pid', 'w').write(str(p.pid))\n"
            "        open('started', 'w').write('x')\n"
            "        time.sleep(300)\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "slow tests")
        git(self.repo, "push", "origin", "main")

    def assert_rolled_back(self, head):
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.repo / "one.txt").exists())
        self.assertEqual(git(self.remote, "rev-parse", "main"), head)
        self.assertEqual(self.tasks()[1]["state"], "claimed")

    def test_sigterm_during_tests_rolls_back_and_kills_children(self):
        self.start()
        self.add_slow_test()
        head = git(self.repo, "rev-parse", "HEAD")
        p = subprocess.Popen(
            [sys.executable, BOARD_PY, "--repo", str(self.repo), "integrate",
             "1", "--branch", "dev/T1", "--session", "A"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, **GIT_ENV))
        marker = self.repo / "started"
        for _ in range(300):
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "tests never started")
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rolled back", err)
        self.assert_rolled_back(head)
        self.assert_dead(int((self.repo / "child.pid").read_text()))

    def assert_dead(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("child process %d survived" % pid)

    def test_test_timeout_kills_whole_process_group(self):
        self.start()
        self.add_slow_test()
        head = git(self.repo, "rev-parse", "HEAD")
        args = board.build_parser().parse_args(
            ["--repo", str(self.repo), "integrate", "1", "--branch",
             "dev/T1", "--session", "A"])
        err = io.StringIO()
        with mock.patch.object(board, "TEST_TIMEOUT", 3), \
                contextlib.redirect_stderr(err):
            code = board.cmd_integrate(args)
        self.assertEqual(code, 1)
        self.assertIn("timed out", err.getvalue())
        self.assert_rolled_back(head)
        self.assert_dead(int((self.repo / "child.pid").read_text()))

    def test_unexpected_exception_rolls_back_and_propagates(self):
        self.start()
        head = git(self.repo, "rev-parse", "HEAD")
        args = board.build_parser().parse_args(
            ["--repo", str(self.repo), "integrate", "1", "--branch",
             "dev/T1", "--session", "A"])
        real = board.git

        def boom(cwd, *a, **kw):
            if a[:1] == ("push",):
                raise KeyboardInterrupt
            return real(cwd, *a, **kw)

        with mock.patch.object(board, "git", boom), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                board.cmd_integrate(args)
        self.assert_rolled_back(head)

    def test_push_rejected_because_origin_moved_has_clear_message(self):
        self.start()
        head = git(self.repo, "rev-parse", "HEAD")
        args = board.build_parser().parse_args(
            ["--repo", str(self.repo), "integrate", "1", "--branch",
             "dev/T1", "--session", "A"])
        real = board.git

        def reject(cwd, *a, **kw):
            if a[:1] == ("push",):
                return 1, " ! [rejected] main -> main (fetch first)\n"
            return real(cwd, *a, **kw)

        err = io.StringIO()
        with mock.patch.object(board, "git", reject), \
                contextlib.redirect_stderr(err):
            self.assertEqual(board.cmd_integrate(args), 1)
        self.assertIn("origin/main moved", err.getvalue())
        self.assertIn("git pull --ff-only origin main", err.getvalue())
        self.assert_rolled_back(head)

    def test_refuses_when_main_behind_origin(self):
        self.start()
        other = Path(self.tmp) / "other"
        git(self.tmp, "clone", str(self.remote), str(other))
        (other / "x.txt").write_text("x\n")
        git(other, "add", "-A")
        git(other, "commit", "-m", "elsewhere")
        git(other, "push", "origin", "main")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1",
                "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("behind origin/main", r.stderr)
        self.assertIn("git pull --ff-only origin main", r.stderr)
        self.assertFalse((self.repo / "one.txt").exists())

    def test_refuses_when_main_ahead_of_origin(self):
        self.start()
        (self.repo / "y.txt").write_text("y\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "unpushed")
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1",
                "--session", "A")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ahead of origin/main", r.stderr)
        self.assertIn("git push origin main", r.stderr)

    def test_requires_task_claimed_by_that_session(self):
        self.start()
        r = cli(self.repo, "integrate", "1", "--branch", "dev/T1",
                "--session", "B")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not claimed by B", r.stderr)
        self.assertFalse((self.repo / "one.txt").exists())
        cli(self.repo, "integrate", "1", "--branch", "dev/T1", expect=2)


class Encoding(TmpCase):
    C_LOCALE = {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
                "PYTHONUTF8": "0", "PYTHONIOENCODING": ""}

    def test_cyrillic_note_and_tail_under_c_locale(self):
        self.init()
        text, basis = "Русская заметка", "основание"
        cli(self.repo, "note", "NOTE", "1", text, "--basis", basis,
            "--session", "A", env=self.C_LOCALE, expect=0)
        r = cli(self.repo, "tail", "--session", "B", env=self.C_LOCALE,
                expect=0)
        self.assertIn(text, r.stdout)
        self.assertIn(basis, r.stdout)
        raw = (self.store() / "journal.md").read_bytes().decode("utf-8")
        self.assertIn(text, raw)

    def test_cyrillic_task_title_roundtrip(self):
        plan = FAKE_PLAN.replace("First thing", "Первая задача")
        (self.repo / "docs" / "plan.md").write_text(plan, encoding="utf-8")
        cli(self.repo, "init", "--from", "docs/plan.md", env=self.C_LOCALE,
            expect=0)
        r = cli(self.repo, "status", env=self.C_LOCALE, expect=0)
        self.assertIn("Первая задача", r.stdout)


class ForceBackup(TmpCase):
    def test_force_backs_up_and_wipes_state_journal_cursors(self):
        self.init()
        cli(self.repo, "claim", "--session", "A", expect=0)
        cli(self.repo, "note", "FACT", "1", "keep me", "--session", "A",
            expect=0)
        cli(self.repo, "tail", "--session", "B", expect=0)
        r = cli(self.repo, "init", "--from", "docs/plan.md", "--force",
                expect=0)
        self.assertIn("backed up", r.stdout)
        backups = list(self.store().glob("backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("keep me", (backups[0] / "journal.md").read_text())
        self.assertTrue((backups[0] / "cursors" / "B").exists())
        self.assertEqual(self.tasks()[1]["state"], "open")
        self.assertFalse((self.store() / "journal.md").exists())
        self.assertFalse((self.store() / "cursors").exists())
        cli(self.repo, "note", "NOTE", "1", "fresh", "--session", "A",
            expect=0)
        r = cli(self.repo, "tail", "--session", "B", expect=0)
        self.assertIn("fresh", r.stdout)
        self.assertNotIn("keep me", r.stdout)

    def test_done_validates_session(self):
        self.init()
        cli(self.repo, "done", "1", "--session", "bad name", expect=1)


class Ci(unittest.TestCase):
    RUNS = {"workflow_runs": [
        {"head_sha": "a" * 40, "status": "in_progress", "conclusion": None,
         "html_url": "https://example/1"},
        {"head_sha": "b" * 40, "status": "completed",
         "conclusion": "success", "html_url": "https://example/2"},
        {"head_sha": "c" * 40, "status": "completed",
         "conclusion": "failure", "html_url": "https://example/3"},
    ]}

    def run_ci(self, *argv, runs=None):
        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "token"
            tok.write_text("SECRET-TOKEN\n")
            seen = {}

            def fake(url, token):
                seen["url"], seen["token"] = url, token
                return runs if runs is not None else self.RUNS

            args = board.build_parser().parse_args(["ci", *argv])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = board.cmd_ci(args, fetch=fake, token_path=tok)
            self.assertNotIn("SECRET-TOKEN", out.getvalue())
            self.assertEqual(seen["token"], "SECRET-TOKEN")
            self.assertIn("/repos/EugeneBerezov/artel/actions/runs?"
                          "branch=main&per_page=5", seen["url"])
            return code, out.getvalue()

    def test_latest_pending_is_4(self):
        code, out = self.run_ci()
        self.assertEqual(code, 4)
        self.assertIn("aaaaaaa in_progress", out)
        self.assertIn("pending", out)
        self.assertNotIn("https://example/3", out)  # other commit

    def test_all_runs_of_the_sha_count(self):
        def run(sha, status, concl):
            return {"head_sha": sha * 40, "status": status,
                    "conclusion": concl, "html_url": "u"}
        ok, bad = run("a", "completed", "success"), run("a", "completed",
                                                         "failure")
        pend = run("a", "queued", None)
        self.assertEqual(self.run_ci(runs={"workflow_runs": [ok, ok]})[0], 0)
        self.assertEqual(self.run_ci(runs={"workflow_runs": [ok, bad]})[0], 1)
        self.assertEqual(self.run_ci(runs={"workflow_runs": [pend, bad]})[0], 1)
        self.assertEqual(self.run_ci(runs={"workflow_runs": [ok, pend]})[0], 4)

    def test_sha_filter_success_and_failure(self):
        self.assertEqual(self.run_ci("--sha", "bbbbbbb")[0], 0)
        self.assertEqual(self.run_ci("--sha", "ccc")[0], 1)

    def test_no_match_is_2(self):
        code, out = self.run_ci("--sha", "ddd")
        self.assertEqual(code, 2)
        self.assertIn("no run yet", out)
        self.assertEqual(self.run_ci(runs={"workflow_runs": []})[0], 2)

    def test_missing_token_file(self):
        args = board.build_parser().parse_args(["ci"])
        with self.assertRaises(board.BoardError):
            board.cmd_ci(args, fetch=lambda u, t: {},
                         token_path=Path("/nonexistent/token"))


if __name__ == "__main__":
    unittest.main()

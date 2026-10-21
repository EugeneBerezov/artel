"""Task 37: runner guards for an unattended night — the `stage_runs` cap (R39), the failure breaker, the
`agent_unavailable` pause and its escalation streak, and the run records of PD-71 (workers, verify wait, idle spans).
Spec §5 step 6, §3.1 Counters; PD-57, PD-58, PD-60, PD-61, PD-71."""
import dataclasses
import json
import re
import sys
import time
from unittest import mock

from artel import journal, pauses
from artel import runner as runner_module
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION, task_branch
from tests.e2e import E2ECase
from tests.helpers import git, py_cmd

MIN = 60.0
FAIL = [{"result": {"outcome": "fail", "summary": "cannot do it"}}]
WRITE = [{"write": ["hello.txt", "hi\n"]}, {"commit": "add hello"},
         {"result": {"outcome": "ready", "summary": "done"}}]
NO_COMMITS = [{"result": {"outcome": "ready", "summary": "nothing to change"}}]
ASK = [{"result": {"outcome": "ask", "summary": "s", "question": "which db?"}}]
PASS = [{"result": {"outcome": "pass", "summary": "ok"}}]
CLEAR = [{"result": {"outcome": "pass", "summary": "clear"}}]
SLEEP = [{"sleep": 30}, {"result": {"outcome": "ready", "summary": "never"}}]


def crash(line="fatal: bad model", code=1):
    return [{"crash": code, "stderr": line}]


class NightCase(E2ECase):
    SETTINGS = dict(max_agents=1, max_attempts=10, max_stage_runs=100, max_consecutive_failures=3,
                    limit_retry="30m", limit_probes=20, quota_stop_five_hour=0.95, quota_stop_seven_day=0.8,
                    night_budget_seven_day=0.15)

    def started(self, **kw):
        r = self.runner(**{**self.SETTINGS, **kw})
        r.start()
        return r

    def active(self):
        return pauses.active(self.board)

    def sent(self, r, kind=None):
        self.assertTrue(r.flush_notifications(10), "notifications not delivered")
        return [e for e in self.events if kind is None or e.kind == kind]

    def set_task(self, tid, **fields):
        task = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state=task.state, expect_version=task.version, **fields))

    def streak(self):
        raw = self.board.get_meta("failure_streak")
        return json.loads(raw) if raw else {"count": 0, "tasks": []}

    def ustreak(self):
        raw = self.board.get_meta("unavailable_streak")
        return json.loads(raw) if raw else {"signature": "", "count": 0}

    def stage_calls(self, stage="write", task=None):
        return [c for c in self.calls() if c["stage"] == stage and (task is None or c["task"] == str(task))]

    def probe_calls(self):
        return [c for c in self.calls() if c["stage"] == "probe"]

    def settle(self, r, until):
        """Ticks until `until()` holds and no stage or verify job is running."""
        self.drive(r, lambda: until() and not any(j.kind in ("stage", "verify") for j in r.jobs))

    def crashed(self, r):
        """Ticks until a crash paused the runner (agent_unavailable, or failures by escalation)."""
        self.settle(r, lambda: {"agent_unavailable", "failures"} & set(self.active()))

    def probe_lifts(self, r):
        """Advances the clock in `limit_retry` steps until a probe ran, then ticks until it lifted the pause. (The
        fake clock stands still within a step, so a probe and the next crash can share a timestamp; probe.due then
        counts that probe for the new pause and wants the next gap.)"""
        n = len(self.probe_calls())
        for _ in range(6):
            self.clock.advance(31 * MIN)
            self.drive(r, lambda: not any(j.kind == "probe" for j in r.jobs))
            if len(self.probe_calls()) > n:
                break
        self.drive(r, lambda: len(self.probe_calls()) == n + 1 and "agent_unavailable" not in self.active()
                   and not any(j.kind == "probe" for j in r.jobs))

    def fail_tasks(self, r, *tids):
        """Each task fails its write once and then sits still (max_attempts = 1 → stuck)."""
        for tid in tids:
            self.settle(r, lambda tid=tid: self.board.get_task(tid).state == "stuck")


class UnavailableTest(NightCase):
    def test_agent_crash_before_first_call_is_unavailable(self):
        """Mutation caught: counting the crash (attempts, stage_runs, the breaker) or resetting the branch (it would
        be a writer's stage_failed, R17); no `agent_unavailable` pause or no notification."""
        tid = self.add_task("crashy")
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        wt = self.tmp / "wt"
        git(self.repo_path, "worktree", "add", "-q", str(wt), task_branch(tid))
        tip = self.commit_file("work.txt", "w\n", "earlier work", cwd=wt)
        git(self.repo_path, "worktree", "remove", "--force", str(wt))
        self.scenario({"write": crash("fatal: cannot start", 3)})
        r = self.started()
        self.crashed(r)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.stage_runs, task.running), ("writing", 0, 0, False))
        self.assertEqual(git(self.repo_path, "rev-parse", task_branch(tid)), tip)
        self.assertEqual(sorted(self.active()), ["agent_unavailable"])
        self.assertEqual(self.active()["agent_unavailable"].detail, "exit 3: fatal: cannot start")
        self.assertIsNone(self.active()["agent_unavailable"].until)
        self.assertIsNone(self.board.get_meta("failure_streak"))
        paused = [e.text for e in self.sent(r, "paused")]
        self.assertEqual(len(paused), 1)
        self.assertTrue(paused[0].startswith("agent_unavailable: exit 3: fatal: cannot start"), paused)
        self.assertEqual(len(self.stage_calls()), 1)   # paused: no second run

    def test_permanent_unavailable_escalates_to_failures(self):
        """PD-61: the same crash after passing probes, `max_consecutive_failures` times → `failures` naming the
        signature. Mutation caught: no escalation (an all-night crash/probe loop); escalating without probes."""
        self.scenario({"write": crash("error: unknown model 'opsu'")})
        r = self.started()
        tid = self.add_task("t")
        self.crashed(r)   # the first crash: no passing probe before it, it does not count
        self.assertEqual(self.ustreak()["count"], 0)
        limit = self.cfg["max_consecutive_failures"]
        for n in range(1, limit + 1):
            self.probe_lifts(r)
            self.crashed(r)
            self.assertEqual(self.ustreak()["count"], n)
            if n < limit:
                self.assertEqual(sorted(self.active()), ["agent_unavailable"])
        self.assertEqual(sorted(self.active()), ["failures"])
        pause = self.active()["failures"]
        signature = "exit 1: error: unknown model 'opsu'"
        self.assertIn(f"agent unavailable {limit} times with {signature}", pause.detail)
        self.assertEqual(pause.causes[-1].setting, "max_consecutive_failures")
        self.assertTrue(any(e.text.startswith("failures:") for e in self.sent(r, "paused")))
        self.assertEqual(self.board.get_task(tid).attempts, 0)   # still no counter for the task
        self.clock.advance(5 * 60 * MIN)
        for _ in range(3):
            r.tick()
        self.assertEqual(len(self.stage_calls()), limit + 1)    # failures is not probed: it waits for a human

    def test_escalation_with_one_task(self):
        """PD-61 amended: one task is enough. Mutation caught: applying BREAKER_MIN_TASKS to the escalation."""
        self.scenario({"write": crash()})
        r = self.started(max_consecutive_failures=2)
        tid = self.add_task("only")
        self.crashed(r)
        for _ in range(2):
            self.probe_lifts(r)
            self.crashed(r)
        self.assertIn("failures", self.active())
        self.assertEqual({c["task"] for c in self.stage_calls()}, {str(tid)})

    def test_signature_ignores_digits(self):
        """Two crashes whose stderr differ only in a pid → one streak. Mutation caught: comparing raw stderr."""
        self.scenario({"1:write#1": crash("killed pid 1234"), "1:write#2": crash("killed pid 5678"),
                       "1:write#3": crash("killed pid 9100"), "write": crash("killed pid 1234")})
        r = self.started()
        self.add_task("t")
        self.crashed(r)
        self.probe_lifts(r)
        self.crashed(r)
        self.probe_lifts(r)
        self.crashed(r)
        self.assertEqual(self.ustreak(), {"signature": "exit 1: killed pid ####", "count": 2})

    def test_unavailable_streak_reset_by_other_outcome(self):
        """Mutation caught: an outcome other than agent_unavailable that leaves the streak (it would escalate)."""
        self.scenario({"1:write#1": crash(), "1:write#2": crash(), "1:write#3": FAIL, "1:write": crash()})
        r = self.started(max_consecutive_failures=2)
        tid = self.add_task("t")
        self.crashed(r)
        self.probe_lifts(r)
        self.crashed(r)                                          # streak 1
        self.assertEqual(self.ustreak()["count"], 1)
        self.probe_lifts(r)                                      # write#3 fails: the streak is reset
        self.settle(r, lambda: self.board.get_task(tid).attempts == 1)
        self.assertEqual(self.ustreak()["count"], 0)
        self.crashed(r)                                          # write#4 crashes: the old probe no longer qualifies
        self.probe_lifts(r)
        self.crashed(r)                                          # write#5: streak 1 again, below 2
        self.assertEqual(self.ustreak()["count"], 1)
        self.assertEqual(sorted(self.active()), ["agent_unavailable"])

    def test_unavailable_streak_reset_by_other_task_outcome(self):
        """PD-61 amended: any other classified outcome of any task resets the streak. Mutation caught: resetting only
        on the crashing task's own outcomes."""
        self.scenario({"1:write": crash(), "2:write": FAIL})
        r = self.started(max_consecutive_failures=2, max_attempts=1)
        a = self.add_task("a")
        self.crashed(r)
        self.probe_lifts(r)
        self.crashed(r)
        self.assertEqual(self.ustreak()["count"], 1)
        self.set_task(a, state="stuck", stuck_from="writing")   # out of the way: task b runs next
        b = self.add_task("b")
        self.probe_lifts(r)
        self.settle(r, lambda: self.board.get_task(b).state == "stuck")
        self.assertEqual(self.ustreak()["count"], 0)
        self.assertNotIn("failures", self.active())

    def test_signature_change_restarts_streak(self):
        """T37 review m2. Kills the mutant `count = count + 1` whatever the signature: A, then B, then B is a streak
        of 2 with B, not 3 (which would escalate)."""
        self.scenario({"1:write#1": crash("fatal: A"), "1:write#2": crash("fatal: A"), "1:write#3": crash("fatal: B"),
                       "1:write": crash("fatal: B")})
        r = self.started()
        self.add_task("t")
        self.crashed(r)                                          # no passing probe before it: not counted
        expected = [("exit 1: fatal: A", 1), ("exit 1: fatal: B", 1), ("exit 1: fatal: B", 2)]
        for signature, count in expected:
            self.probe_lifts(r)
            self.crashed(r)
            self.assertEqual(self.ustreak(), {"signature": signature, "count": count})
        self.assertEqual(sorted(self.active()), ["agent_unavailable"])

    def test_probe_before_a_restart_does_not_qualify(self):
        """PD-61, T37 review m2. Kills the mutant that drops `_unavailable_reset_at = now` from `start()`: a probe that
        passed before a restart would count the first crash after it."""
        self.scenario({"write": crash()})
        r = self.started()
        pauses.add(self.board, "agent_unavailable", "exit 1: fatal: bad model", now=self.clock())
        self.probe_lifts(r)
        self.assertEqual(json.loads(self.board.get_meta("last_probe"))["outcome"], "ok")
        r.shutdown()
        self.clock.advance(MIN)
        r2 = self.started()
        self.add_task("t")
        self.crashed(r2)
        self.assertEqual(len(self.stage_calls()), 1)
        self.assertEqual(self.ustreak()["count"], 0)

    def test_parallel_unavailable_after_one_probe_counts_once(self):
        """Controller decision on T37 review m1. Kills the mutant that counts every run after the same passing probe:
        three crashes in parallel after one probe would escalate at once (count 3 = max_consecutive_failures)."""
        self.scenario({"write": crash()})
        r = self.started(max_agents=3)
        for name in ("a", "b", "c"):
            self.add_task(name, area=[name])
        self.crashed(r)                                          # the first wave: no passing probe before it
        before = len(self.stage_calls())
        self.probe_lifts(r)
        self.crashed(r)
        self.assertEqual(len(self.stage_calls()) - before, 3)    # all three ran after the one probe
        self.assertEqual(self.ustreak(), {"signature": "exit 1: fatal: bad model", "count": 1})
        self.assertEqual(sorted(self.active()), ["agent_unavailable"])


class CannotStartTest(NightCase):
    """Owner 2026-10-21: an agent command that cannot be started is `agent_unavailable`; a probe of it fails."""

    def unresolvable(self, r):
        """Runner.start refuses an unusable agent_command: it goes missing only after the start."""
        original = r.cfg
        missing = str(self.tmp / "gone" / "claude")
        r.cfg = dataclasses.replace(r.cfg, values={**r.cfg.values, "agent_command": [missing]})
        return original, missing

    def test_missing_agent_binary_is_unavailable_without_attempt(self):
        """Mutation caught: a missing agent binary taken as an internal error (an attempt per pick, the breaker, no
        pause: a night of instant failures), or a slot or run record touched for it."""
        self.scenario({"write": WRITE, "review": PASS})
        r = self.started()
        original, missing = self.unresolvable(r)
        tid = self.add_task("t")
        self.crashed(r)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.stage_runs, task.running), ("writing", 0, 0, False))
        signature = re.sub(r"\d", "#", f"cannot find {missing} on PATH")[:200]
        self.assertEqual(sorted(self.active()), ["agent_unavailable"])
        self.assertEqual(self.active()["agent_unavailable"].detail, signature)
        self.assertEqual(self.board.list_runs(tid), [])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.repo.branch_exists(task_branch(tid)))
        self.assertIsNone(self.board.get_meta("failure_streak"))
        fails = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "FAIL"]
        self.assertEqual(fails, [f"agent unavailable, requeued without a counter: {signature}"])
        self.assertEqual([e.text for e in self.sent(r, "paused")], [f"agent_unavailable: {signature}"])
        r.cfg = original                                     # the binary is back: a probe lifts, the stage runs
        self.probe_lifts(r)
        self.settle(r, lambda: self.board.get_task(tid).state not in ("open", "writing"))
        self.assertEqual(self.board.get_task(tid).attempts, 0)
        self.assertEqual(len(self.stage_calls()), 1)

    def test_probe_that_cannot_start_counts_as_failed(self):
        """Mutation caught: a probe whose agent cannot start taken as an internal error, or not counted (probes for
        ever), or lifting the pause."""
        r = self.started(limit_probes=2)
        self.unresolvable(r)
        pauses.add(self.board, "agent_unavailable", "exit 1: x", now=self.clock())
        for _ in range(4):
            self.clock.advance(61 * MIN)
            self.drive(r, lambda: not any(j.kind == "probe" for j in r.jobs))
        pause = self.active()["agent_unavailable"]
        self.assertTrue(pause.exhausted)
        self.assertEqual(len(pauses.probe_times(self.board, now=self.clock())), 2)
        self.assertEqual(json.loads(self.board.get_meta("last_probe"))["outcome"], "unavailable")
        self.assertEqual(self.probe_calls(), [])
        fails = [row["text"] for row in self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'")]
        self.assertEqual(len([t for t in fails if t.startswith("probe: the agent cannot start: cannot find")]), 2)
        self.assertEqual([t for t in fails if "internal error" in t], [])
        self.assertTrue(any("`artel resume` is needed" in e.text for e in self.sent(r, "paused")))


class CapTest(NightCase):
    def test_due_at_cap_goes_stuck_without_running(self):
        """R39. Mutation caught: starting the run although `stage_runs` reached the cap (a third agent call)."""
        self.scenario({"write": FAIL})
        r = self.started(max_stage_runs=2)
        tid = self.add_task("loops")
        self.settle(r, lambda: self.board.get_task(tid).state == "stuck")
        task = self.board.get_task(tid)
        self.assertEqual((task.stage_runs, task.attempts, task.stuck_from), (2, 2, "writing"))
        self.assertEqual(len(self.stage_calls()), 2)
        self.assertEqual(len(self.board.list_runs(tid)), 2)   # no run row for the refused start
        verdicts = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "VERDICT"]
        self.assertTrue(any(v.startswith("STUCK write") and "max_stage_runs=2" in v for v in verdicts), verdicts)
        stuck = self.sent(r, "stuck")
        self.assertEqual([e.task_id for e in stuck], [tid])
        for _ in range(3):
            r.tick()
        self.assertEqual(len(self.stage_calls()), 2)

    def test_answer_resets_stage_runs_and_runs_once(self):
        """`answer` sets stage_runs = 0: the task runs once more, then the cap holds again. Mutation caught: a cap
        check that ignores the reset (no run after the answer) or no cap after it (a review would run)."""
        self.scenario({"1:write#1": ASK, "write": WRITE, "review": PASS})
        r = self.started(max_stage_runs=1)
        tid = self.add_task("asks")
        self.settle(r, lambda: self.board.get_task(tid).state == "blocked")
        self.assertEqual(self.board.get_task(tid).stage_runs, 1)
        self.board.add_request("answer", tid, {"text": "postgres"})
        self.settle(r, lambda: self.board.get_task(tid).state == "stuck")
        self.assertEqual(len(self.stage_calls("write")), 2)
        self.assertEqual(self.stage_calls("review"), [])
        task = self.board.get_task(tid)
        self.assertEqual((task.stuck_from, task.stage_runs), ("reviewing", 1))

    def test_attack_task_with_failing_writer_ends_stuck(self):
        """An --attack task loops R10a → R11 (attempts = 0) → R17 → open; only stage_runs bounds it. Mutation
        caught: stage_runs+1 missing on the attack or the write path."""
        self.scenario({"attack": CLEAR, "write": FAIL})
        cap = 5
        r = self.started(max_stage_runs=cap, max_attempts=2)
        tid = self.add_task("attacked", attack=True)
        self.settle(r, lambda: self.board.get_task(tid).state == "stuck")
        task = self.board.get_task(tid)
        agent_runs = len(self.stage_calls("attack")) + len(self.stage_calls("write"))
        self.assertLessEqual(agent_runs, cap)
        self.assertEqual(task.stage_runs, agent_runs)
        verdicts = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "VERDICT"]
        self.assertTrue(any("max_stage_runs" in v for v in verdicts), verdicts)


class BreakerTest(NightCase):
    def test_breaker_needs_min_tasks(self):
        """PD-60 a, the rule from runner.BREAKER_MIN_TASKS. Mutation caught: one task tripping the breaker."""
        limit = 3
        self.scenario({"write": FAIL})
        r = self.started(max_consecutive_failures=limit, max_attempts=limit)
        a = self.add_task("a")
        self.fail_tasks(r, a)                                    # `limit` failures of one task
        self.assertEqual(self.streak(), {"count": limit, "tasks": [a]})
        self.assertEqual("failures" in self.active(), runner_module.BREAKER_MIN_TASKS <= 1)
        b = self.add_task("b")
        self.settle(r, lambda: "failures" in self.active())
        self.assertEqual(self.streak()["tasks"], [a, b][:max(2, runner_module.BREAKER_MIN_TASKS)])
        pause = self.active()["failures"]
        self.assertTrue(pause.detail.startswith(f"{self.streak()['count']} failed stages in a row: #{a}, #{b}"),
                        pause.detail)
        self.assertEqual(pause.causes[-1].setting, "max_consecutive_failures")
        self.assertTrue(any(e.text.startswith("failures:") for e in self.sent(r, "paused")))

    def test_breaker_reset_by_a_success(self):
        """Mutation caught: a success that leaves the count (2 + 2 failures from two tasks would trip)."""
        self.scenario({"1:write": FAIL, "2:write": WRITE, "2:review": FAIL})
        r = self.started(max_attempts=2)
        a = self.add_task("a")
        self.fail_tasks(r, a)
        self.assertEqual(self.streak()["count"], 2)
        b = self.add_task("b")
        self.settle(r, lambda: self.board.get_task(b).state == "stuck")   # write ready (reset), review fails twice
        self.assertEqual(self.streak(), {"count": 2, "tasks": [b]})
        self.assertNotIn("failures", self.active())

    def test_breaker_ignores_requeue_and_unavailable(self):
        """Mutation caught: agent_unavailable or a requeue counted as a failure, or resetting the count."""
        self.scenario({"1:write": FAIL, "2:write": crash(), "3:write": [{"result_error": 429}]})
        r = self.started(max_attempts=1)
        a = self.add_task("a")
        self.fail_tasks(r, a)
        before = {"count": 1, "tasks": [a]}
        self.assertEqual(self.streak(), before)
        b = self.add_task("b")
        self.crashed(r)
        self.assertEqual(self.streak(), before)
        self.set_task(b, state="stuck", stuck_from="writing")
        pauses.remove(self.board, "agent_unavailable")
        c = self.add_task("c")
        self.settle(r, lambda: "limit" in self.active())
        self.assertEqual((self.board.get_task(c).state, self.board.get_task(c).attempts), ("writing", 0))
        self.assertEqual(self.streak(), before)

    def test_breaker_streak_survives_restart(self):
        """Mutation caught: an in-memory count (after the restart two more failures would not trip)."""
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1)
        a = self.add_task("a")
        self.fail_tasks(r, a)
        r.shutdown()
        r2 = self.started(max_attempts=1)
        b = self.add_task("b")
        self.fail_tasks(r2, b)
        self.assertNotIn("failures", self.active())
        c = self.add_task("c")
        self.fail_tasks(r2, c)
        self.assertIn("failures", self.active())
        self.assertEqual(self.streak(), {"count": 3, "tasks": [a, b, c]})

    def test_resume_resets_breaker(self):
        """Task 18's resume of `failures` sets the count to 0. Mutation caught: a count kept (c would trip again)."""
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1, max_consecutive_failures=2)
        a, b = self.add_task("a"), self.add_task("b")
        self.fail_tasks(r, a, b)
        self.assertIn("failures", self.active())
        self.board.add_request("resume", None, {"force": False})
        r.tick()
        self.assertNotIn("failures", self.active())
        self.assertEqual(self.streak(), {"count": 0, "tasks": []})
        c = self.add_task("c")
        self.fail_tasks(r, c)
        self.assertNotIn("failures", self.active())
        self.assertEqual(self.streak(), {"count": 1, "tasks": [c]})

    def test_write_ready_no_commits_counts_for_breaker(self):
        """R17r is a failed stage. Mutation caught: a no-commit writer looping all night uncounted."""
        self.scenario({"write": NO_COMMITS})
        r = self.started(max_attempts=1, max_consecutive_failures=2)
        a, b = self.add_task("a"), self.add_task("b")
        self.fail_tasks(r, a, b)
        self.assertEqual(self.streak(), {"count": 2, "tasks": [a, b]})
        self.assertIn("failures", self.active())

    def test_internal_errors_count_for_breaker(self):
        """T37 review m2: an internal error of a stage job increments `attempts` (R20), so it counts. Kills the
        mutant whose `failed` leaves out "internal_error" (a night of internal errors would never trip)."""
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1, max_consecutive_failures=2)
        with mock.patch.object(runner_module.stages, "validate_result", side_effect=RuntimeError("boom")):
            a, b = self.add_task("a"), self.add_task("b")
            self.fail_tasks(r, a, b)
        self.assertEqual(self.streak(), {"count": 2, "tasks": [a, b]})
        self.assertIn("failures", self.active())

    def test_verify_fix_without_commits_resets_breaker(self):
        """R15b asks a human: not a failure (spec §3.1 Counters). T37 review m2: kills the mutant that counts every
        `ready_no_commits` without the `attempts` check (here a + b would trip the breaker)."""
        self.scenario({"1:write": FAIL})
        r = self.started(max_attempts=1, max_consecutive_failures=2)
        a = self.add_task("a")
        self.fail_tasks(r, a)
        self.assertEqual(self.streak(), {"count": 1, "tasks": [a]})
        b = self.add_task("b")
        git(self.repo_path, "branch", task_branch(b), INTEGRATION)
        self.set_task(b, state="fixing", fix_kind="verify", round=1,
                      fix_base=git(self.repo_path, "rev-parse", task_branch(b)))
        self.scenario({"1:write": FAIL, f"{b}:fix": NO_COMMITS})
        self.settle(r, lambda: self.board.get_task(b).state == "blocked")
        self.assertEqual(self.board.get_task(b).asked_by, "verify")   # R15b
        self.assertEqual(self.streak(), {"count": 0, "tasks": []})
        self.assertNotIn("failures", self.active())

    def test_setup_failure_leaves_both_streaks(self):
        """A setup failure neither counts nor resets (spec §5 step 6). T37 review m2: kills the mutant `if tr is None:`
        in `_agent_outcome` (a setup failure would reset the failure count and the unavailable streak)."""
        flag = self.tmp / "setup-breaks"
        self.write_toml({"commands.setup": py_cmd(f"import pathlib, sys; sys.exit(1 if pathlib.Path({str(flag)!r})"
                                                  ".exists() else 0)")})
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1)
        a = self.add_task("a")
        self.fail_tasks(r, a)
        self.assertEqual(self.streak(), {"count": 1, "tasks": [a]})
        unavailable = {"signature": "exit 1: fatal: bad model", "count": 1}
        self.board.set_meta("unavailable_streak", json.dumps(unavailable))
        flag.write_text("x", encoding="utf-8")
        self.board.set_slot_setup_hash(1, None)                  # the next stage runs setup again, and it fails
        b = self.add_task("b")
        self.settle(r, lambda: "setup_failed" in self.active())
        self.assertEqual(self.board.get_task(b).attempts, 0)
        self.assertEqual(self.streak(), {"count": 1, "tasks": [a]})
        self.assertEqual(self.ustreak(), unavailable)


class CounterTest(NightCase):
    def test_internal_error_after_agent_ran_counts_stage_run(self):
        """Spec §3.1 Counters: a run counts when its agent started and it was not requeued — also when an internal
        error follows (T37 review m4). Mutation caught: `agent_started` not passed to finish_internal_error (no
        `stage_runs`), or set for every stage job (task b, whose stage failed before the spawn, would count)."""
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1)
        a = self.add_task("a")
        with mock.patch.object(runner_module.stages, "validate_result", side_effect=RuntimeError("boom")):
            self.settle(r, lambda: self.board.get_task(a).state == "stuck")
        task = self.board.get_task(a)
        self.assertEqual((task.attempts, task.stage_runs, len(self.stage_calls())), (1, 1, 1))
        b = self.add_task("b")
        with mock.patch.object(runner_module.stages, "plan_stage", side_effect=ArtelError("boom before the spawn")):
            self.settle(r, lambda: self.board.get_task(b).state == "stuck")
        task = self.board.get_task(b)
        self.assertEqual((task.attempts, task.stage_runs, len(self.stage_calls())), (1, 0, 1))

    def test_failed_finish_after_agent_ran_counts_stage_run(self):
        """T37 review m4, the other path: applying the result raised (`_finish_failed`). Mutation caught: that path
        charging the internal error without `stage_runs`."""
        self.scenario({"write": FAIL})
        r = self.started(max_attempts=1)
        a = self.add_task("a")
        with mock.patch.object(runner_module.stages, "record_stage", side_effect=RuntimeError("boom")):
            self.settle(r, lambda: self.board.get_task(a).state == "stuck")
        task = self.board.get_task(a)
        self.assertEqual((task.attempts, task.stage_runs, len(self.stage_calls())), (1, 1, 1))


class RunRecordTest(NightCase):
    def test_runs_record_workers(self):
        """PD-71 a. Mutation caught: workers not passed to plan_stage (NULL), or a constant."""
        self.scenario({"write": FAIL})
        r = self.started(max_agents=3, max_attempts=1)
        tid = self.add_task("t")
        self.settle(r, lambda: self.board.get_task(tid).state == "stuck")
        self.assertEqual([run.workers for run in self.board.list_runs(tid)], [3])

    def test_verify_queued_at_recorded(self):
        """PD-71 c: two tasks due for verify at once. Mutation caught: stamping queued_at at job start (the second's
        wait would be ~0) or not passing it (NULL)."""
        self.clock = time.time   # runs.started_at is wall time
        self.write_toml({"commands.verify": py_cmd("import time; time.sleep(1)")})
        tids = []
        for title in ("first", "second"):
            tid = self.add_task(title, area=[title])
            git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
            self.set_task(tid, state="verifying", round=1)
            tids.append(tid)
        r = self.started(max_agents=2)
        self.settle(r, lambda: all(self.board.get_task(t).state != "verifying" for t in tids))
        runs = sorted((run for t in tids for run in self.board.list_runs(t) if run.stage == "verify"),
                      key=lambda run: run.started_at)
        self.assertEqual(len(runs), 2)
        first, second = runs
        self.assertIsNotNone(first.queued_at)
        self.assertLessEqual(first.queued_at, first.started_at)
        self.assertGreaterEqual(second.started_at - second.queued_at, first.ended_at - first.started_at)

    def last_reasons(self):
        latest = {}
        for span in self.board.idle_spans():
            latest[span.slot] = span
        return {n: (s.reason, s.until) for n, s in latest.items()}

    def test_idle_reason_area_deps_no_task_paused(self):
        """PD-71 b, one reason after another on one board (max_agents = 2). Mutation caught: a wrong reason, a pause
        ignored, or the reason computed from other inputs than the pick's."""
        self.scenario({"write": SLEEP})
        r = self.started(max_agents=2)
        with self.subTest("no_task"):
            r.tick()
            self.assertEqual(self.last_reasons(), {1: ("no_task", None), 2: ("no_task", None)})
        with self.subTest("paused"):
            pauses.add(self.board, "manual", "test", now=self.clock())
            pauses.add(self.board, "disk", "test", now=self.clock())
            r.tick()
            self.assertEqual(self.last_reasons(), {1: ("paused:manual", None), 2: ("paused:manual", None)})
            pauses.remove(self.board, "manual")
            pauses.remove(self.board, "disk")
        with self.subTest("deps"):
            a = self.add_task("a")
            self.set_task(a, state="blocked")
            self.add_task("b", after=[a])
            r.tick()
            self.assertEqual(self.last_reasons(), {1: ("deps", None), 2: ("deps", None)})
        with self.subTest("area"):
            c = self.add_task("c", area=["x"])
            self.add_task("d", area=["x"])
            r.tick()
            self.assertEqual([j.task_id for j in r.jobs], [c])
            busy_slot = r.jobs[0].slot
            reasons = self.last_reasons()
            self.assertIsNotNone(reasons[busy_slot][1])   # the slot that got a job: its span is closed
            self.assertEqual(reasons[3 - busy_slot], ("area", None))
        r.shutdown()
        self.assertTrue(all(s.until is not None for s in self.board.idle_spans()))   # shutdown closes spans

    def test_idle_span_written_only_on_change(self):
        """Mutation caught: a span written per tick; a crashed runner's open span left open."""
        r = self.started()
        for _ in range(10):
            r.tick()
        spans = self.board.idle_spans()
        self.assertEqual([(s.slot, s.reason, s.until) for s in spans], [(1, "no_task", None)])
        self.board.open_idle(2, "deps", self.clock() - 50)   # as left by a crashed runner
        r.shutdown()
        self.clock.advance(100)
        r2 = self.started()
        left = [s for s in self.board.idle_spans() if s.slot == 2]
        self.assertEqual([(s.since, s.until) for s in left], [(self.clock() - 150, self.clock() - 150)])
        r2.shutdown()


if __name__ == "__main__":   # pragma: no cover
    import unittest
    unittest.main(argv=[sys.argv[0]])

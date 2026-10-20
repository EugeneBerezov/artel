import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from artel import events, integrate, journal, platform, slots, stages
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION
from artel.runner import Runner
from tests.e2e import E2ECase
from tests.helpers import git, py_cmd, subprocess_env

WRITE = [{"write": ["hello.txt", "hi\n"]}, {"commit": "add hello"},
         {"result": {"outcome": "ready", "summary": "done"}}]
PASS = [{"result": {"outcome": "pass", "summary": "ok"}}]


def kinds(e):
    return [x.kind for x in e.events]


class RunnerCoreTest(E2ECase):
    def run_to_done(self, tid, runner=None, timeout=40):
        r = runner or self.runner(max_agents=1)
        r.start()
        try:
            self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=timeout)
        finally:
            r.shutdown()

    def test_happy_path_one_slot(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        self.run_to_done(tid)
        self.assertTrue(self.repo.is_ancestor(self.commit_of("add hello"), "artel/integration"))
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))
        self.assertIn("DONE", [e.kind for e in journal.task_entries(self.board, tid)])
        self.assertSlotsCleanAndDetached()
        self.assertEqual(kinds(self), ["done"])
        self.assertEqual(self.events[0].task_id, tid)

    def test_review_findings_fix_then_done(self):
        tid = self.add_task("Add hello")
        self.scenario({
            "write": WRITE,
            f"{tid}:review#1": [{"result": {"outcome": "findings", "summary": "s", "findings": [
                {"severity": "major", "text": "needs a trailing newline", "basis": "hello.txt:1"}]}}],
            # entry ids are shared by the whole journal: #1 is the write VERDICT, so the finding is #2
            f"{tid}:review#2": [{"result": {"outcome": "pass", "summary": "closed", "closed": [2]}}],
            "fix": [{"write": ["hello.txt", "hi there\n"]}, {"commit": "fix hello"},
                    {"result": {"outcome": "ready", "summary": "fixed"}}]})
        self.run_to_done(tid)
        self.assertTrue(self.repo.is_ancestor(self.commit_of("fix hello"), "artel/integration"))
        stages_called = [c["stage"] for c in self.calls()]
        self.assertEqual(stages_called, ["write", "review", "fix", "review"])
        self.assertEqual(journal.open_findings(self.board, tid), [])

    def test_verify_red_fix_then_done(self):
        self.write_toml({"commands.verify": py_cmd("import os, sys; sys.exit(0 if os.path.exists('fixed.txt') else 1)")})
        tid = self.add_task("Add hello")
        self.scenario({"write": WRITE, "review": PASS,
                       "fix": [{"write": ["fixed.txt", "yes\n"]}, {"commit": "make verify green"},
                               {"result": {"outcome": "ready", "summary": "fixed"}}]})
        self.run_to_done(tid, self.runner(max_agents=1))
        self.assertEqual([c["stage"] for c in self.calls()], ["write", "review", "fix"])   # R15: a verify fix re-verifies
        self.assertEqual(self.board.get_task(tid).verify_fixes, 1)

    def test_two_tasks_two_slots_disjoint_areas(self):
        def work(name):
            return [{"sleep": 1.0}, {"write": [name, "x\n"]}, {"commit": f"work {name}"},
                    {"result": {"outcome": "ready", "summary": "done"}}]

        a = self.add_task("A", area=["a"])
        b = self.add_task("B", area=["b"])
        self.scenario({f"{a}:write": work("a.txt"), f"{b}:write": work("b.txt"), "review": PASS})
        r = self.runner(max_agents=2)
        r.start()
        try:
            self.drive(r, lambda: all(self.board.get_task(t).state == "done" for t in (a, b)), timeout=60)
        finally:
            r.shutdown()
        writes = [(row.task_id, row.slot, row.started_at, row.ended_at) for row in self.board.all_runs(stages=["write"])]
        self.assertEqual({w[1] for w in writes}, {1, 2})
        self.assertLess(max(w[2] for w in writes), min(w[3] for w in writes))   # the two writers overlapped
        self.assertSlotsCleanAndDetached()

    def test_overlapping_areas_never_run_together(self):
        def work(name):
            return [{"sleep": 0.5}, {"write": [name, "x\n"]}, {"commit": f"work {name}"},
                    {"result": {"outcome": "ready", "summary": "done"}}]

        a = self.add_task("A", area=["src"])
        b = self.add_task("B", area=["src/deep"])
        self.scenario({f"{a}:write": work("a.txt"), f"{b}:write": work("b.txt"), "review": PASS})
        r = self.runner(max_agents=2)
        r.start()
        try:
            self.drive(r, lambda: all(self.board.get_task(t).state == "done" for t in (a, b)), timeout=60)
        finally:
            r.shutdown()
        first, second = sorted(self.board.all_runs(stages=["write"]), key=lambda row: row.started_at)
        self.assertLessEqual(first.ended_at, second.started_at)   # PD-6: serialized

    def test_second_runner_exits_with_error(self):
        r = self.runner()
        r.start()
        result = subprocess.run([sys.executable, "-m", "artel", "run"], cwd=self.repo_path, capture_output=True,
                                encoding="utf-8", errors="replace", env=subprocess_env(), timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another artel runner", result.stderr)
        r.shutdown()
        again = self.runner()
        again.start()   # the lock was released by shutdown
        again.shutdown()

    def test_missing_agent_cli_fails_fast(self):
        r = self.runner(agent_command=["definitely-no-such-claude-binary"])
        with self.assertRaises(ArtelError) as cm:
            r.start()
        self.assertIn("agent_command", str(cm.exception))
        ok = self.runner(agent_command=[sys.executable, str(__import__("tests.e2e", fromlist=["x"]).FAKE_AGENT)])
        ok.start()   # the failed start released the lock
        ok.shutdown()

    def test_job_exception_counts_attempt_and_records_fail(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        real = stages.run_stage
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom in the worker")
            return real(*a, **kw)

        with mock.patch.object(stages, "run_stage", flaky):
            r = self.runner(max_agents=1)
            r.start()
            try:
                self.drive(r, lambda: "error" in kinds(self))
                task = self.board.get_task(tid)
                self.assertEqual((task.attempts, task.running), (1, False))
                (fail,) = [e for e in journal.task_entries(self.board, tid) if e.kind == "FAIL"]
                self.assertIn("internal error", fail.text)
                self.assertIn("boom in the worker", fail.text)
                err = [e for e in self.events if e.kind == "error"][0]
                self.assertNotIn("Traceback", err.text)           # PD-38: never the traceback
                self.assertIn(f"artel show {tid}", err.text)
                self.drive(r, lambda: self.board.get_task(tid).state == "done")   # the stage runs again
            finally:
                r.shutdown()

    def test_setup_failure_pauses_and_keeps_branch(self):
        self.write_toml({"commands.setup": py_cmd("import sys; sys.exit(3)")})
        self.scenario({"write": WRITE, "review": PASS})
        first = self.add_task("first")
        second = self.add_task("second", area=["other"])
        r = self.runner(max_agents=2)
        r.start()
        try:
            self.drive(r, lambda: self.board.get_meta("pause_reason") == "setup_failed")
            for _ in range(5):   # nothing new is picked while paused
                r.tick()
                r.wait(0.05)
            self.assertEqual(self.board.get_task(second).state, "open")
            task = self.board.get_task(first)
            self.assertEqual((task.state, task.attempts), ("writing", 0))
            self.assertTrue(self.repo.branch_exists(f"artel/{first}"))
            self.assertIn("paused", kinds(self))
            self.assertEqual(self.calls(), [])   # the agent never started
        finally:
            r.shutdown()

    def test_proposed_tasks_wait_without_vet_support(self):
        self.scenario({"write": WRITE, "review": PASS})
        proposed = transitions_create(self.board, "add_agent", title="agent idea", created_by="agent:1")
        tid = self.add_task("Add hello")
        self.run_to_done(tid)
        self.assertEqual(self.board.get_task(proposed).state, "proposed")   # it did not starve the open task

    def test_proposed_task_picked_into_vetting_when_vet_supported(self):
        proposed = transitions_create(self.board, "add_agent", title="agent idea", created_by="agent:1")
        r = self.runner(max_agents=1)
        with mock.patch.object(stages, "SUPPORTED_STAGES", {"attack", "write", "review", "fix", "vet"}):
            r.start()
            try:
                r.tick()
                self.assertEqual(self.board.get_task(proposed).state, "vetting")   # R03
            finally:
                r.shutdown()

    def test_base_moved_merged_before_next_pick(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        self.commit_file("base-news.txt", "news\n", "base moved on")   # main moves after integration was created
        r = self.runner(max_agents=1)
        r.start()
        try:
            r.tick()
            self.assertEqual([j.kind for j in r.jobs], ["base"])
            self.assertEqual(self.board.get_task(tid).state, "open")   # not picked while the base update is pending
            for _ in range(3):
                r.tick()
                if not r.jobs:
                    break
                r.wait(0.2)
            self.drive(r, lambda: self.board.get_task(tid).state == "done")
        finally:
            r.shutdown()
        self.assertTrue(self.repo.is_ancestor("main", INTEGRATION))
        self.assertIsNone(self.board.get_meta("base_running"))

    def test_base_job_error_pauses_without_task(self):
        tid = self.add_task("Add hello")
        self.commit_file("base-news.txt", "news\n", "base moved on")
        with mock.patch.object(integrate, "base_update", side_effect=RuntimeError("base broke")):
            r = self.runner(max_agents=1)
            r.start()
            try:
                self.drive(r, lambda: self.board.get_meta("pause_reason") == "base_conflict")
            finally:
                r.shutdown()
        self.assertTrue(self.board.get_meta("pause_detail").startswith("internal error:"))
        row = self.board.conn.execute("SELECT task_id, kind, text FROM entries WHERE kind = 'FAIL'").fetchone()
        self.assertIsNone(row["task_id"])
        self.assertIn("base broke", row["text"])
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts), ("open", 0))   # PD-34: no task was charged
        self.assertIn("paused", kinds(self))

    def test_task_dispatched_once_until_running(self):
        # catches busy=set(): the same task would be dispatched again on the next tick, before `running` is set
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        real = Runner._on_start

        def slow(self_, board, job):
            inner = real(self_, board, job)

            def delayed(*args):
                time.sleep(1.0)   # `running` is only set after this
                inner(*args)
            return delayed

        spawned = []
        real_spawn = Runner._spawn

        def counting(self_, kind, task_id, slot, work):
            spawned.append((kind, self.board.get_task(task_id).state))
            return real_spawn(self_, kind, task_id, slot, work)

        with mock.patch.object(Runner, "_on_start", slow), mock.patch.object(Runner, "_spawn", counting):
            r = self.runner(max_agents=2)
            r.start()
            try:
                for _ in range(12):
                    r.tick()
                    time.sleep(0.1)
                self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=60)
            finally:
                r.shutdown()
        # one dispatch of the write stage although `running` was not set yet (later stages are separate jobs)
        self.assertEqual([state for kind, state in spawned].count("writing"), 1)
        self.assertEqual([c["stage"] for c in self.calls()].count("write"), 1)

    def test_branch_checked_out_elsewhere_skipped(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        git(self.repo_path, "branch", f"artel/{tid}", INTEGRATION)
        other = self.tmp / "human-worktree"
        git(self.repo_path, "worktree", "add", str(other), f"artel/{tid}")
        r = self.runner(max_agents=1)
        r.start()
        try:
            for _ in range(5):
                r.tick()
                r.wait(0.05)
            task = self.board.get_task(tid)
            self.assertEqual((task.state, task.attempts), ("open", 0))   # PD-8: nothing applied
            self.assertEqual(self.calls(), [])
            git(self.repo_path, "worktree", "remove", "--force", str(other))
            self.drive(r, lambda: self.board.get_task(tid).state == "done")
        finally:
            r.shutdown()

    def test_runner_env_reaches_fake_agent(self):
        import os
        self.assertNotIn("ARTEL_FAKE_DIR", os.environ)
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        self.run_to_done(tid)
        self.assertGreaterEqual(len(self.calls()), 2)   # only Runner(env=...) carried ARTEL_FAKE_DIR

    def test_requeue_after_restart_has_no_counter(self):
        self.scenario({"write": [{"sleep": 30}]})
        tid = self.add_task("Add hello")
        r = self.runner(max_agents=1)
        r.start()
        self.drive(r, lambda: self.board.get_task(tid).running)
        self.assertEqual(self.board.get_task(tid).state, "writing")
        r.shutdown()
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))
        again = self.runner(max_agents=1)
        again.start()
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))
        again.shutdown()
        self.assertSlotsCleanAndDetached()

    def test_start_clears_a_stale_running_flag(self):
        tid = self.add_task("Add hello")
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=task.version, state="writing")
        self.board.set_running(tid, kind="agent", pid=999999, group=999999, fingerprint=None, slot=1)
        self.board.set_meta("base_running", "{}")
        r = self.runner()
        r.start()
        try:
            self.assertFalse(self.board.get_task(tid).running)
            self.assertIsNone(self.board.get_meta("base_running"))
            self.assertEqual(self.board.get_meta("runner_pid"), str(__import__("os").getpid()))
        finally:
            r.shutdown()

    def test_shutdown_is_idempotent_and_releases_the_lock(self):
        r = self.runner()
        r.start()
        r.shutdown()
        r.shutdown()
        lock = platform.FileLock(self.repo.lock_path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_blocked_task_notifies_once(self):
        self.scenario({"write": [{"result": {"outcome": "ask", "summary": "s", "question": "which db?"}}]})
        tid = self.add_task("Add hello")
        r = self.runner(max_agents=1)
        r.start()
        try:
            self.drive(r, lambda: self.board.get_task(tid).state == "blocked")
            for _ in range(3):
                r.tick()
        finally:
            r.shutdown()
        self.assertEqual([e.kind for e in self.events], ["blocked"])
        self.assertEqual(self.events[0].task_id, tid)

    def test_event_json(self):
        e = events.Event("done", 3, "t", "done")
        self.assertEqual(e.to_json(), '{"kind": "done", "task_id": 3, "title": "t", "state": "done"}')
        self.assertEqual(events.EVENT_KINDS[-1], "error")
        events.null_notify(e)


class RunnerHardeningTest(E2ECase):
    """Review round 1 of Task 15."""

    def make_runner(self, **kw):
        r = self.runner(**kw)
        r.start()
        return r

    def test_a_task_held_by_a_human_does_not_stop_other_work(self):
        # PD-8: the held task is skipped, its slot is not wasted
        self.scenario({"write": WRITE, "review": PASS})
        held = self.add_task("held", area=["a"])
        other = self.add_task("other", area=["b"])
        git(self.repo_path, "branch", f"artel/{held}", INTEGRATION)
        git(self.repo_path, "worktree", "add", str(self.tmp / "human"), f"artel/{held}")
        r = self.make_runner(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(other).state == "done", timeout=60)
            self.assertEqual(self.board.get_task(held).state, "open")
        finally:
            r.shutdown()

    def test_base_update_waits_while_integration_is_checked_out(self):
        # PD-13: no base job is started every tick, and the board keeps working
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        self.commit_file("base-news.txt", "news\n", "base moved on")
        git(self.repo_path, "worktree", "add", str(self.tmp / "human-int"), INTEGRATION)
        spawned = []
        real = Runner._spawn

        def counting(self_, kind, task_id, slot, work):
            spawned.append(kind)
            return real(self_, kind, task_id, slot, work)

        with mock.patch.object(Runner, "_spawn", counting):
            r = self.make_runner(max_agents=1)
            try:
                self.drive(r, lambda: self.board.get_task(tid).state == "verified", timeout=60)
                for _ in range(10):
                    r.tick()
                    r.wait(0.05)
                self.assertNotIn("base", spawned)
                self.assertEqual(self.board.get_task(tid).state, "verified")   # integrate_task: held (PD-13)
                git(self.repo_path, "worktree", "remove", "--force", str(self.tmp / "human-int"))
                self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=60)
            finally:
                r.shutdown()
        self.assertIn("base", spawned)

    def test_nothing_new_is_picked_while_the_base_job_runs(self):
        # catches: _base_moved returning False once the base job was started, and ignoring a running verify
        self.write_toml({"commands.verify": py_cmd("import time; time.sleep(2)")})
        self.scenario({"write": WRITE, "review": PASS})
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("int-only.txt", "i\n", "integration has its own commit")
        git(self.repo_path, "checkout", "-q", "main")
        self.commit_file("base-news.txt", "news\n", "base moved on")   # diverged: a merge + verify is needed
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=2)
        try:
            r.tick()
            self.assertEqual([j.kind for j in r.jobs], ["base"])
            self.wait_until(lambda: self.board.get_meta("base_running"))
            info = json.loads(self.board.get_meta("base_running"))
            self.assertEqual(set(info), {"pid", "group", "fingerprint", "slot", "started_at"})   # PD-34
            self.assertTrue(platform.pid_alive(info["pid"]))
            for _ in range(4):
                r.tick()
                self.assertEqual([j.kind for j in r.jobs], ["base"])   # a free slot, but nothing new is picked
                self.assertEqual(self.board.get_task(tid).state, "open")
                r.wait(0.1)
            self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=90)
            self.assertIsNone(self.board.get_meta("base_running"))
        finally:
            r.shutdown()

    def test_base_conflict_pauses_with_an_event_and_no_task_is_charged(self):
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "integration\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        self.commit_file("shared.txt", "main\n", "main edit")
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_meta("pause_reason") == "base_conflict")
        finally:
            r.shutdown()
        self.assertIn("paused", kinds(self))
        self.assertIn("conflict", self.board.get_meta("pause_detail"))
        self.assertEqual(self.board.get_task(tid).attempts, 0)

    def test_a_failure_while_finishing_a_job_is_charged_as_an_internal_error(self):
        # catches: only writing a FAIL entry (the stage would run, and spend quota, forever)
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        with mock.patch.object(stages, "finish_stage", side_effect=RuntimeError("cannot apply")):
            r = self.make_runner(max_agents=1)
            try:
                self.drive(r, lambda: self.board.get_task(tid).state == "stuck", timeout=60)
            finally:
                r.shutdown()
        task = self.board.get_task(tid)
        self.assertEqual((task.attempts, task.running), (2, False))   # max_attempts 2
        self.assertEqual([c["stage"] for c in self.calls()].count("write"), 2)
        self.assertIn("error", kinds(self))
        self.assertIn("stuck", kinds(self))

    def test_a_failing_integrate_is_recorded_once_and_not_retried_every_tick(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=1)
        try:
            with mock.patch.object(integrate, "integrate_task", side_effect=RuntimeError("branch vanished")) as m:
                self.drive(r, lambda: self.board.get_task(tid).state == "verified")
                for _ in range(8):
                    r.tick()
                    r.wait(0.05)
                self.assertEqual(m.call_count, 1)
            fails = [e for e in journal.task_entries(self.board, tid) if e.kind == "FAIL"]
            self.assertEqual(len(fails), 1)
            self.clock.advance(61)   # the backoff is clock-based
            self.drive(r, lambda: self.board.get_task(tid).state == "done")
        finally:
            r.shutdown()

    def test_an_error_inside_a_tick_step_does_not_end_the_runner(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=1)
        try:
            with mock.patch.object(pick_module(), "select", side_effect=RuntimeError("select broke")):
                for _ in range(4):
                    r.tick()
            fails = [e for e in self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'")]
            self.assertEqual(len(fails), 1)   # recorded once per distinct text
            self.assertIn("select broke", fails[0]["text"])
            self.drive(r, lambda: self.board.get_task(tid).state == "done")
        finally:
            r.shutdown()

    def test_slot_busy_in_a_worker_is_skipped_without_a_counter(self):
        tid = self.add_task("Add hello")
        calls = []
        real = stages.plan_stage

        def busy_once(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise slots.SlotBusy(self.tmp / "elsewhere", "artel/1")
            return real(*a, **kw)

        self.scenario({"write": WRITE, "review": PASS})
        with mock.patch.object(stages, "plan_stage", busy_once):
            r = self.make_runner(max_agents=1)
            try:
                self.drive(r, lambda: self.board.get_task(tid).state == "done")
            finally:
                r.shutdown()
        self.assertGreaterEqual(len(calls), 2)                     # the busy job, then the real one
        task = self.board.get_task(tid)
        self.assertEqual(task.attempts, 0)                         # PD-8: skipped, no counter
        self.assertEqual([e for e in journal.task_entries(self.board, tid) if e.kind == "FAIL"], [])
        self.assertEqual(kinds(self), ["done"])                    # no error event

    def test_answered_flag_is_cleared_when_the_stage_starts(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=task.version, state="writing", answered=True)
        r = self.make_runner(max_agents=1)
        try:
            r.tick()
            self.assertFalse(self.board.get_task(tid).answered)
        finally:
            r.shutdown()

    def test_no_new_jobs_after_a_soft_stop(self):
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=1)
        try:
            r.request_stop(now=False)
            for _ in range(3):
                r.tick()
            self.assertEqual(r.jobs, [])
            self.assertEqual(self.board.get_task(tid).state, "open")
        finally:
            r.shutdown()

    def test_start_prepares_the_shim_and_clears_runner_pid_on_shutdown(self):
        r = self.make_runner()
        self.assertTrue((self.repo.artel_dir / "bin").is_dir())
        self.assertEqual(self.board.get_meta("runner_pid"), str(os.getpid()))
        r.shutdown()
        self.assertIsNone(self.board.get_meta("runner_pid"))

    def test_verify_and_setup_processes_are_dead_after_shutdown(self):
        # the harness records every pid a job started, not only the agents'
        self.write_toml({"commands.verify": py_cmd("import time; time.sleep(30)")})
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.make_runner(max_agents=1)
        self.drive(r, lambda: any(j.kind == "verify" for j in r.jobs) and self.board.get_task(tid).running)
        self.wait_until(lambda: len(self.started_pids) >= 3)
        pids = set(self.started_pids)
        r.shutdown()
        self.wait_until(lambda: not any(platform.pid_alive(p) for p in pids), timeout=15)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("verifying", 0, False))   # requeued, no counter


def pick_module():
    from artel import pick
    return pick


def transitions_create(board, event, **kw):
    from artel import transitions
    return transitions.create(board, event, **kw)


if __name__ == "__main__":
    unittest.main()

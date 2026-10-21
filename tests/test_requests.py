import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from artel import board as B, cli, integrate, journal, pauses, platform, requests
from artel import runner as runner_module
from artel.gitutil import INTEGRATION, Repo, task_branch
from tests.e2e import E2ECase
from tests.helpers import git, subprocess_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SLEEP = [{"sleep": 30}, {"result": {"outcome": "ready", "summary": "never"}}]
WRITE = [{"write": ["hello.txt", "hi\n"]}, {"commit": "add hello"},
         {"result": {"outcome": "ready", "summary": "done"}}]
PASS = [{"result": {"outcome": "pass", "summary": "ok"}}]


class Hooks:
    """A RunnerHooks stand-in that records the calls."""

    def __init__(self):
        self.calls = []

    def terminate(self, task_id):
        self.calls.append(("terminate", task_id))

    def stop(self, now):
        self.calls.append(("stop", now))

    def resumed(self, kinds):
        self.calls.append(("resumed", list(kinds)))


class RequestsCase(E2ECase):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        os.chdir(self.repo_path)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def set_task(self, tid, **fields):
        task = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state=task.state, expect_version=task.version, **fields))

    def blocked(self, asked_by, cause="ask", **fields):
        tid = self.add_task("Ask me", area=["a"])
        self.set_task(tid, state="blocked", asked_by=asked_by, cause=cause, round=1, **fields)
        ask = journal.append(self.board, "ASK", "which db?", author=f"agent:{asked_by}", task_id=tid)
        return tid, ask

    def branch(self, tid, commit=True):
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        if commit:
            wt = self.tmp / f"wt{tid}"
            git(self.repo_path, "worktree", "add", str(wt), task_branch(tid))
            (wt / f"f{tid}.txt").write_text("x\n", encoding="utf-8", newline="\n")
            git(wt, "add", ".")
            git(wt, "commit", "-m", f"work {tid}")
            git(self.repo_path, "worktree", "remove", "--force", str(wt))
        return git(self.repo_path, "rev-parse", task_branch(tid))

    def apply(self, hooks=None):
        return requests.apply_pending(self.repo, self.board, self.cfg, hooks or Hooks())

    def enqueue(self, command, task_id=None, **payload):
        return requests.enqueue(self.repo, self.board, command, task_id, payload)

    def request(self, n):
        return self.board.get_request(n)

    def started(self, **kw):
        r = self.runner(**kw)
        r.start()
        return r


class RequestCommandTest(RequestsCase):
    def test_commands_only_enqueue(self):
        tid, _ = self.blocked("write")
        code, out, _ = self.cli("answer", str(tid), "postgres")
        self.assertEqual(code, 0)
        self.assertIn(f"queued: answer #{tid}", out)
        self.assertIn("no runner is running; it applies when `artel run` starts", out)
        self.assertEqual(self.board.get_task(tid).state, "blocked")
        (req,) = self.board.pending_requests()
        self.assertEqual((req.command, req.task_id, req.payload), ("answer", tid, {"text": "postgres"}))

    def test_unknown_task_is_a_usage_error(self):
        code, _, err = self.cli("approve", "99")
        self.assertEqual(code, 2)
        self.assertIn("no such task: #99", err)
        self.assertEqual(self.board.pending_requests(), [])

    def test_reject_without_reason_stores_empty_string(self):
        tid = self.add_task("t")
        self.assertEqual(self.cli("reject", str(tid))[0], 0)
        self.assertEqual(self.board.pending_requests()[0].payload, {"reason": ""})

    def test_resume_force_payload(self):
        self.cli("resume", "--force")
        self.cli("resume")
        self.assertEqual([r.payload for r in self.board.pending_requests()], [{"force": True}, {"force": False}])

    def test_human_commands_refuse_under_artel_child(self):
        # catches a handler without the check (PD-65 b)
        tid = self.add_task("t")
        argvs = {"answer": ["answer", str(tid), "x"], "approve": ["approve", str(tid)],
                 "reject": ["reject", str(tid)], "accept": ["accept", str(tid)],
                 "rework": ["rework", str(tid), "--note", "n"], "pause": ["pause"], "resume": ["resume"],
                 "stop": ["stop"]}
        self.assertEqual(sorted(argvs), sorted(requests.COMMANDS))
        r = self.started()
        try:
            with mock.patch.dict(os.environ, {"ARTEL_CHILD": "1"}):
                for command, argv in argvs.items():
                    with self.subTest(command=command):
                        code, _, err = self.cli(*argv)
                        self.assertEqual(code, 2)
                        self.assertIn(f"artel {command} is a human command; refused inside a process started by "
                                      "artel (ARTEL_CHILD is set)", err)
            self.assertEqual(self.board.pending_requests(), [])
            self.assertFalse(self.repo.stop_path.exists())
        finally:
            r.shutdown()

    def test_accept_prints_verdict_and_diffstat(self):
        tid = self.add_task("t")
        self.branch(tid)
        self.set_task(tid, state="awaiting_accept")
        journal.append(self.board, "VERDICT", "merge=ask: a human merges", author="runner", task_id=tid)
        journal.append(self.board, "FAIL", "flaky verify: red then green, log x.log", author="runner", task_id=tid)
        code, out, _ = self.cli("accept", str(tid))
        self.assertEqual(code, 0)
        self.assertIn("verdict: merge=ask: a human merges", out)
        self.assertIn(f"f{tid}.txt", out)
        self.assertIn("flaky verify: red then green", out)
        self.assertLess(out.index(f"f{tid}.txt"), out.index("request #"))   # the evidence comes first

    def test_approve_without_branch_prints_no_diff(self):
        tid = self.add_task("t")
        self.set_task(tid, state="awaiting_approval")
        journal.append(self.board, "FAIL", "agent crashed", author="runner", task_id=tid)
        _, out, _ = self.cli("approve", str(tid))
        self.assertNotIn("diff", out)
        self.assertNotIn("agent crashed", out)   # only a flaky-verify FAIL is evidence


class ApplyTest(RequestsCase):
    def test_answer_writes_answer_entry_and_resumes(self):
        tid, ask = self.blocked("write")
        n = self.enqueue("answer", tid, text="postgres")
        (applied,) = self.apply()
        self.assertIsNone(applied.error)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.answered, task.asked_by), ("writing", True, None))
        (answer,) = [e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"]
        self.assertEqual((answer.text, answer.ref, answer.author), ("postgres", ask, "human"))
        self.assertIsNotNone(self.request(n).applied_at)
        self.assertEqual(self.apply(), [])   # applied once

    def test_answer_refers_to_the_latest_ask(self):
        tid, first = self.blocked("write")
        latest = journal.append(self.board, "ASK", "and the port?", author="agent:write", task_id=tid)
        self.assertNotEqual(first, latest)
        self.enqueue("answer", tid, text="5432")
        self.apply()
        (answer,) = [e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"]
        self.assertEqual(answer.ref, latest)

    def test_a_busy_board_leaves_the_request_pending(self):
        # catches: finishing the request with "database is locked" (the human's answer would be lost)
        tid, _ = self.blocked("write")
        n = self.enqueue("answer", tid, text="postgres")
        with mock.patch.object(requests.transitions, "apply", side_effect=B.BoardBusy("database is locked")):
            self.assertEqual(self.apply(), [])
        self.assertIsNone(self.request(n).applied_at)
        self.assertEqual(self.board.get_task(tid).state, "blocked")
        self.assertEqual([e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"], [])
        (applied,) = self.apply()   # the next tick applies it
        self.assertIsNone(applied.error)
        self.assertEqual(self.board.get_task(tid).state, "writing")

    def test_the_transition_and_finish_request_commit_together(self):
        # catches finish_request outside the transaction: a busy finish would leave the answer applied but the
        # request pending, and the next tick would apply it a second time
        tid, _ = self.blocked("write")
        n = self.enqueue("answer", tid, text="postgres")
        with mock.patch.object(self.board, "finish_request", side_effect=B.BoardBusy("database is locked")):
            self.assertEqual(self.apply(), [])
        self.assertIsNone(self.request(n).applied_at)
        self.assertEqual(self.board.get_task(tid).state, "blocked")
        self.assertEqual([e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"], [])

    def test_a_failing_git_effect_does_not_fail_the_applied_request(self):
        # catches: recording the request as an error although the task is already rejected
        tid = self.add_task("t")
        self.branch(tid)
        n = self.enqueue("reject", tid, reason="no")
        with mock.patch.object(requests.slots, "apply_git_effects", side_effect=RuntimeError("branch busy")):
            (applied,) = self.apply()
        self.assertIsNone(applied.error)
        self.assertIsNone(self.request(n).error)
        self.assertEqual(self.board.get_task(tid).state, "rejected")
        fails = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "FAIL"]
        self.assertTrue(any("branch busy" in t for t in fails), fails)

    def test_a_failed_transition_finishes_the_request_once(self):
        # the transaction rolled back: no ANSWER entry, the request carries the error and is not re-applied
        tid = self.add_task("t")
        n = self.enqueue("rework", tid, note="again")
        (applied,) = self.apply()
        self.assertIsNotNone(applied.error)
        self.assertEqual(self.request(n).error, applied.error)
        self.assertEqual([e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"], [])
        self.assertEqual(self.apply(), [])

    def test_answer_reviewer_question_returns_to_review(self):
        tid, _ = self.blocked("review")
        self.enqueue("answer", tid, text="yes")
        self.apply()
        self.assertEqual(self.board.get_task(tid).state, "reviewing")

    def test_invalid_request_recorded_as_error(self):
        tid = self.add_task("t")
        n = self.enqueue("answer", tid, text="nobody asked")
        (applied,) = self.apply()
        self.assertIn("transition not allowed", applied.error)
        self.assertEqual(self.request(n).error, applied.error)
        self.assertEqual(self.board.get_task(tid).state, "open")
        self.assertEqual([e for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"], [])

    def test_requests_apply_in_id_order(self):
        tid = self.add_task("t")
        self.set_task(tid, state="awaiting_approval")
        self.enqueue("approve", tid)
        self.enqueue("reject", tid, reason="no")
        self.apply()
        self.assertEqual(self.board.get_task(tid).state, "rejected")

    def test_approve_rejected_by_vet_only(self):
        for rejected_by, state in (("vet", "open"), ("human", "rejected")):
            with self.subTest(rejected_by=rejected_by):
                tid = self.add_task("t")
                self.set_task(tid, state="rejected", rejected_by=rejected_by)
                self.enqueue("approve", tid)
                (applied,) = self.apply()
                self.assertEqual(self.board.get_task(tid).state, state)
                self.assertEqual(applied.error is None, state == "open")

    def test_reject_reason_recorded(self):
        tid = self.add_task("t")
        self.branch(tid)
        self.enqueue("reject", tid, reason="too big")
        hooks = Hooks()
        self.apply(hooks)
        self.assertEqual(self.board.get_task(tid).state, "rejected")
        answers = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"]
        self.assertEqual(answers, ["reject: too big"])   # PD-19
        self.assertFalse(self.repo.branch_exists(task_branch(tid)))
        self.assertEqual(hooks.calls, [("terminate", tid)])

    def test_reject_not_allowed_terminates_nothing(self):
        tid = self.add_task("t")
        self.set_task(tid, state="done")
        self.enqueue("reject", tid)
        hooks = Hooks()
        (applied,) = self.apply(hooks)
        self.assertIsNotNone(applied.error)
        self.assertEqual(hooks.calls, [])

    def test_rework_stuck_with_and_without_branch(self):
        with_branch = self.add_task("with")
        tip = self.branch(with_branch)
        self.set_task(with_branch, state="stuck", stuck_from="fixing", attempts=2)
        without = self.add_task("without")
        self.set_task(without, state="stuck", stuck_from="writing", attempts=2)
        self.enqueue("rework", with_branch, note="split it")
        self.enqueue("rework", without, note="try again")
        self.apply()
        task = self.board.get_task(with_branch)
        self.assertEqual((task.state, task.fix_base, task.attempts), ("fixing", tip, 0))   # PD-35: fix_base=tip
        self.assertEqual(self.board.get_task(without).state, "writing")
        answers = [e.text for e in journal.task_entries(self.board, without) if e.kind == "ANSWER"]
        self.assertEqual(answers, ["rework: try again"])

    def test_accept_blocked_verify_ask_goes_to_verifying_with_an_answer(self):
        """Task 31 (R35b) replaced test_accept_on_blocked_is_an_error_until_r35b. Mutation caught: no ANSWER entry,
        or `accepted` set (accept would then bypass the merge gates, PD-62)."""
        tid, _ = self.blocked("verify")
        self.enqueue("accept", tid)
        (applied,) = self.apply()
        self.assertIsNone(applied.error)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.fix_kind, task.asked_by, task.accepted), ("verifying", None, None, False))
        answers = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "ANSWER"]
        self.assertEqual(answers, ["accept: verify again as is"])

    def test_accept_on_a_review_blocked_task_is_an_error(self):
        tid, _ = self.blocked("review", cause="cap")
        self.enqueue("accept", tid)
        (applied,) = self.apply()
        self.assertIn("transition not allowed", applied.error)
        self.assertEqual(self.board.get_task(tid).state, "blocked")

    def test_pause_manual_then_resume(self):
        self.enqueue("pause")
        self.apply()
        self.assertEqual(pauses.active(self.board)["manual"].detail, "artel pause")
        hooks = Hooks()
        self.enqueue("resume", force=False)
        (applied,) = self.apply(hooks)
        self.assertIsNone(applied.error)
        self.assertEqual(pauses.active(self.board), {})
        self.assertEqual(hooks.calls, [("resumed", ["manual"])])

    def test_resume_refusal_finishes_the_request_with_its_error(self):
        pauses.add(self.board, "quota", "seven_day", now=1.0, until=10.0)
        n = self.enqueue("resume", force=False)
        hooks = Hooks()
        self.apply(hooks)
        self.assertEqual(self.request(n).error, "pause quota ends by itself; use resume --force")
        self.assertIn("quota", pauses.active(self.board))
        self.assertEqual(hooks.calls, [])

    def test_resume_force_through_a_request(self):
        pauses.add(self.board, "quota", "seven_day", now=1.0, until=10.0)
        pauses.add(self.board, "disk", "low", now=1.0)
        self.enqueue("resume", force=True)
        hooks = Hooks()
        self.apply(hooks)
        self.assertEqual(pauses.active(self.board), {})
        self.assertEqual(hooks.calls, [("resumed", ["quota", "disk"])])

    def test_stop_request_calls_the_hook_and_deletes_the_stop_file(self):
        self.repo.stop_path.write_text("now", encoding="utf-8")
        self.enqueue("stop", now=True)
        hooks = Hooks()
        self.apply(hooks)
        self.assertEqual(hooks.calls, [("stop", True)])
        self.assertFalse(self.repo.stop_path.exists())

    def test_base_conflict_goes_through_pause_set(self):
        old = git(self.repo_path, "rev-parse", INTEGRATION)
        bu = integrate.BaseUpdate("conflict", old, None, None, "c.txt")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "paused")
        self.assertEqual(pauses.active(self.board)["base_conflict"].detail, "conflict: c.txt")
        self.assertIsNone(self.board.get_meta("pause_reason"))


class RunnerRequestsTest(RequestsCase):
    def test_runner_applies_on_next_tick(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid, _ = self.blocked("write")
        r = self.started(max_agents=1)
        try:
            _, out, _ = self.cli("answer", str(tid), "postgres")
            self.assertIn("the runner applies it on its next tick", out)
            self.assertEqual(self.board.get_task(tid).state, "blocked")
            r.tick()
            self.assertEqual(self.board.pending_requests(), [])
            self.assertEqual(self.board.get_task(tid).state, "writing")
            self.assertEqual([j.task_id for j in r.jobs], [tid])   # the same tick picks the answered task
        finally:
            r.shutdown()

    def test_reject_while_running_terminates_first(self):
        self.scenario({"write": SLEEP})
        tid = self.add_task("t")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).running_pid)
            pid = self.board.get_task(tid).running_pid
            self.assertEqual(self.cli("reject", str(tid), "--reason", "wrong idea")[0], 0)
            r.tick()
            task = self.board.get_task(tid)
            self.assertEqual((task.state, task.running, task.attempts), ("rejected", False, 0))
            self.assertFalse(platform.pid_alive(pid))
            self.assertFalse(self.repo.branch_exists(task_branch(tid)))
            for _ in range(3):   # the terminated job's result is never applied
                r.tick()
            self.assertEqual(self.board.get_task(tid).state, "rejected")
            self.assertEqual(r.jobs, [])
            self.assertSlotsCleanAndDetached()
        finally:
            r.shutdown()

    def test_terminate_discards_a_finished_but_uncollected_result(self):
        # catches: applying a terminated job's result (the reject would be followed by a stale `ready`), and
        # terminate leaving `running` set until the next collect
        self.scenario({"write": WRITE})
        tid = self.add_task("t")
        r = self.started(max_agents=1)
        try:
            r.tick()
            (job,) = r.jobs
            job.thread.join(30)          # the stage is over; its result waits for the next collect
            self.assertIsNotNone(job.result)
            r.terminate(tid)
            self.assertFalse(self.board.get_task(tid).running)
            r.tick()
            task = self.board.get_task(tid)
            self.assertEqual((task.state, task.attempts), ("writing", 0))   # not reviewing: nothing applied
        finally:
            r.shutdown()

    def test_accept_moves_to_verified_then_done(self):
        tid = self.add_task("t")
        tip = self.branch(tid)
        self.set_task(tid, state="awaiting_accept")
        self.board.set_meta(f"verified:{tid}", tip)   # PD-45: the verified commit
        self.enqueue("accept", tid)
        r = self.started(max_agents=1)
        try:
            r.tick()
            self.assertEqual(self.board.get_task(tid).state, "done")
        finally:
            r.shutdown()
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), tip)

    def test_resume_request_notifies_resumed(self):
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        pauses.add(self.board, "failures", "3 in a row", now=1.0)
        self.enqueue("resume", force=False)
        r = self.started()
        try:
            r.tick()
        finally:
            r.shutdown()
        self.assertEqual([(e.kind, e.text) for e in self.events if e.kind == "resumed"],
                         [("resumed", "manual"), ("resumed", "failures")])

    def test_pause_request_notifies_paused_and_stops_picking(self):
        tid = self.add_task("t")
        self.enqueue("pause")
        r = self.started(max_agents=1)
        try:
            r.tick()
            r.tick()
            self.assertEqual(r.jobs, [])
            self.assertEqual(self.board.get_task(tid).state, "open")
        finally:
            r.shutdown()
        # §7: the kind, then the cause
        self.assertEqual([(e.kind, e.text) for e in self.events], [("paused", "manual: artel pause")])

    def test_legacy_pause_meta_converted_once(self):
        # catches: reading pause_reason after start (the gate must read the pause set only)
        tid = self.add_task("t")
        self.board.set_meta("pause_reason", "manual")
        self.board.set_meta("pause_detail", "old pause")
        r = self.started(max_agents=1)
        try:
            self.assertEqual(pauses.active(self.board)["manual"].detail, "old pause")
            self.assertIsNone(self.board.get_meta("pause_reason"))
            r.tick()
            self.assertEqual(r.jobs, [])
            pauses.remove(self.board, "manual")
            self.board.set_meta("pause_reason", "manual")   # a stray legacy key no longer pauses
            r.tick()
            self.assertEqual([j.task_id for j in r.jobs], [tid])
        finally:
            r.shutdown()

    def test_stop_request_and_stop_file(self):
        for via in ("request", "file"):
            with self.subTest(via=via):
                r = self.started()
                try:
                    if via == "request":
                        self.enqueue("stop", now=False)
                    else:
                        self.repo.stop_path.write_text("soft", encoding="utf-8")
                    r.tick()
                    self.assertTrue(r.stopping)
                    self.assertFalse(self.repo.stop_path.exists())
                    self.assertEqual(self.board.pending_requests(), [])
                finally:
                    r.shutdown()

    def test_stop_now_requeues_without_counting(self):
        self.scenario({"write": SLEEP})
        tid = self.add_task("t")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).running_pid)
            code, out, _ = self.cli("stop", "--now")
            self.assertEqual(code, 0)
            self.assertIn("queued: stop --now", out)
            self.assertEqual(self.repo.stop_path.read_text(encoding="utf-8"), "now")
            self.drive(r, lambda: not r.jobs, timeout=30)
            self.assertTrue(r.stopping)
            task = self.board.get_task(tid)
            self.assertEqual((task.state, task.running, task.attempts), ("writing", False, 0))
        finally:
            r.shutdown()

    def test_stop_without_runner_leaves_nothing(self):
        code, out, _ = self.cli("stop")
        self.assertEqual((code, out.strip()), (0, "no runner is running"))
        self.assertEqual(self.board.pending_requests(), [])
        self.assertFalse(self.repo.stop_path.exists())
        r = self.started()
        try:
            r.tick()
            self.assertFalse(r.stopping)
        finally:
            r.shutdown()

    def test_reject_of_a_job_that_will_not_stop_fails_and_deletes_nothing(self):
        tid = self.add_task("t")
        self.branch(tid)
        self.set_task(tid, state="writing", round=1)
        r = self.started(kill_grace="1s")
        release = threading.Event()
        thread = threading.Thread(target=release.wait, args=(30,), daemon=True)
        thread.start()
        try:
            r.jobs.append(runner_module.Job(kind="stage", task_id=tid, slot=1, thread=thread,
                                            stop=threading.Event()))
            n = self.enqueue("reject", tid, reason="no")
            with mock.patch.object(runner_module.procs, "JOIN_DEADLINE", 0.1):
                r.tick()
            self.assertIn("did not stop", self.request(n).error)
            self.assertEqual(self.board.get_task(tid).state, "writing")
            self.assertTrue(self.repo.branch_exists(task_branch(tid)))
            self.assertFalse(r.jobs[0].discarded)
        finally:
            release.set()
            thread.join(5)
            r.jobs.clear()
            r.shutdown()

    def test_start_finishes_only_stale_stop_requests(self):
        tid, _ = self.blocked("write")
        n = self.enqueue("answer", tid, text="x")
        r = self.started()
        try:
            self.assertIsNone(self.request(n).applied_at)
        finally:
            r.shutdown()

    def test_stale_stop_file_cleared_at_start(self):
        self.repo.stop_path.write_text("soft", encoding="utf-8")
        n = self.enqueue("stop", now=False)
        r = self.started()
        try:
            self.assertFalse(self.repo.stop_path.exists())
            self.assertEqual(self.request(n).error, "stale: no runner")
            r.tick()
            self.assertFalse(r.stopping)
        finally:
            r.shutdown()


class RunnerRunningTest(RequestsCase):
    def test_runner_running_follows_pid_and_fingerprint(self):
        self.assertFalse(requests.runner_running(self.repo, self.board))
        r = self.started()
        try:
            self.assertTrue(requests.runner_running(self.repo, self.board))
            self.assertTrue(requests.runner_running(self.repo, None))
            fp = self.board.get_meta("runner_fp")
            self.board.set_meta("runner_fp", "another process")   # a reused pid
            self.assertFalse(requests.runner_running(self.repo, self.board))
            self.board.set_meta("runner_fp", fp)
        finally:
            r.shutdown()
        self.assertFalse(requests.runner_running(self.repo, self.board))
        self.assertIsNone(self.board.get_meta("runner_fp"))

    def test_runner_running_does_not_take_the_lock(self):
        # catches a lock probe: a runner starting meanwhile would fail with "another artel runner"
        errors = []
        go = threading.Event()

        def probe():
            go.wait(5)
            try:
                for _ in range(200):
                    requests.runner_running(self.repo, None)
            except Exception as exc:   # noqa: BLE001 - asserted below
                errors.append(exc)

        thread = threading.Thread(target=probe)
        thread.start()
        go.set()
        try:
            for _ in range(5):
                r = self.runner()
                r.start()
                r.shutdown()
        finally:
            thread.join(30)
        self.assertEqual(errors, [])

    def test_runner_without_a_fingerprint_counts_by_pid(self):
        self.board.set_meta("runner_pid", str(os.getpid()))
        self.board.set_meta("runner_fp", "")   # process_fingerprint() gave None at start
        self.assertTrue(requests.runner_running(self.repo, self.board))
        self.board.del_meta("runner_fp")       # pid without fingerprint: a runner still starting
        self.assertFalse(requests.runner_running(self.repo, self.board))

    def test_task_command_creates_no_board(self):
        other = self.tmp / "noboard"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        old = Path.cwd()
        os.chdir(other)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.main(["answer", "1", "x"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 2)
        self.assertIn("no such task: #1", err.getvalue())
        self.assertFalse(Repo.discover(other).board_path.exists())

    def test_runner_running_never_touches_the_lock(self):
        r = self.started()
        try:
            with mock.patch.object(platform.FileLock, "acquire", side_effect=AssertionError("lock probed")), \
                    mock.patch.object(platform.FileLock, "release", side_effect=AssertionError("lock released")):
                self.assertTrue(requests.runner_running(self.repo, self.board))
                self.assertTrue(requests.runner_running(self.repo, None))
        finally:
            r.shutdown()

    def test_runner_running_creates_nothing_without_a_board(self):
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        repo = Repo.discover(other)
        self.assertFalse(requests.runner_running(repo, None))
        self.assertFalse(repo.board_path.exists())
        self.assertFalse(repo.artel_dir.exists())


class ArtelChildSuiteTest(unittest.TestCase):
    def test_suite_ignores_artel_child_from_parent(self):
        # PD-75: tests/__init__.py drops ARTEL_CHILD, so the dogfood runner's verify of artel stays green
        env = subprocess_env(ARTEL_CHILD="1")
        result = subprocess.run([sys.executable, "-m", "unittest", "tests.test_requests.RequestCommandTest", "-q"],
                                cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()

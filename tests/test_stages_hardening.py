"""Stage edge cases found in review round 1: races, exception safety, accounting."""
import json
import threading
import unittest
from unittest import mock

from artel import journal, slots, stages
from artel.gitutil import INTEGRATION
from tests.factories import make_task
from tests.helpers import git, py_cmd
from tests.test_stages import READY, StagesCase, noop


class HardeningTest(StagesCase):
    def conflict_task(self):
        tid = self.task_in("fixing", fix_kind="conflict")
        self.make_branch(tid, name="shared.txt", content="mine\n")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "theirs\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        return tid

    def move_integration(self):
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("other-task.txt", "x\n", "another task merged")
        git(self.repo_path, "checkout", "-q", "main")

    def test_conflict_fix_survives_integration_moving_meanwhile(self):
        # catches: requiring the CURRENT integration tip to be an ancestor (the fix would fail, the branch be deleted)
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"commit": "resolve"},
                                     {"result": READY}]})
        moved = []

        def on_start(kind, pid, group, fp):
            if kind == "agent" and not moved:
                moved.append(True)
                self.move_integration()

        plan, run, tr = self.go(tid, on_start=on_start)
        self.assertTrue(run.merge_committed)
        self.assertEqual(self.task(tid).state, "reviewing")
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))

    def test_conflict_fix_with_a_plain_commit_is_not_a_merge(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"git": ["merge", "--abort"]}, {"write": ["shared.txt", "mine+\n"]},
                                     {"commit": "plain commit"}, {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.new_commits)
        self.assertFalse(run.merge_committed)   # a commit, but not the merge of integration
        self.assertEqual(self.task(tid).state, "open")

    def test_conflict_fix_committed_before_a_requeue_keeps_the_work(self):
        # catches: judging the merge against where THIS run started (a retry would delete the whole branch)
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"commit": "resolve"},
                                     {"result_error": 429}]})
        plan, run, tr = self.go(tid)
        self.assertIsNone(tr)   # requeued: the limit hit came without a valid result
        self.assertTrue(run.merge_committed)
        self.scenario({f"{tid}:fix": [{"result": READY}]})   # the retry: the merge is already in the branch
        plan, run, tr = self.go(tid)
        self.assertEqual(plan.conflict_paths, ())
        self.assertTrue(run.merge_committed)
        self.assertEqual(self.task(tid).state, "reviewing")
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))

    def test_conflict_fix_with_a_follow_up_commit_after_the_merge_is_ready(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"commit": "resolve"},
                                     {"write": ["notes.txt", "n\n"]}, {"commit": "follow-up"}, {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.merge_committed)
        self.assertEqual(self.task(tid).state, "reviewing")

    def test_second_conflict_cycle_needs_a_new_merge(self):
        # an old merge commit on the branch must not satisfy a later conflict fix
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"commit": "resolve"}, {"result": READY}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "reviewing")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "theirs again\n", "integration edits it again")
        git(self.repo_path, "checkout", "-q", "main")
        task = self.task(tid)
        self.board.update_task(tid, expect_state="reviewing", expect_version=task.version, state="fixing",
                               fix_kind="conflict", fix_base=None)
        self.scenario({f"{tid}:fix": [{"result": READY}]})   # claims ready without committing the new merge
        plan, run, tr = self.go(tid)
        self.assertEqual(plan.conflict_paths, ("shared.txt",))
        self.assertFalse(run.merge_committed)

    def test_conflict_fix_that_merges_nothing_is_not_ready(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)
        self.assertFalse(run.new_commits)

    def test_plan_stage_failure_after_prepare_leaves_the_slot(self):
        tid = self.conflict_task()
        with mock.patch.object(slots, "start_conflict_merge", side_effect=slots.GitError(["merge"], "boom")):
            with self.assertRaises(slots.GitError):
                stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        self.assert_slot_left()
        tid2 = self.task_in("writing")
        with mock.patch.object(stages.prompts, "render", side_effect=RuntimeError("template broke")):
            with self.assertRaises(RuntimeError):
                stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid2), 1, base_env=self.base_env)
        self.assert_slot_left()

    def test_findings_and_closures_of_a_fail_or_ask_result_are_not_recorded(self):
        self.configure(review_rounds=5)
        tid = self.task_in("reviewing", round=1, with_commit=True)
        old = journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="old",
                                  basis="", author="agent:review")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "fail", "summary": "gave up", "closed": [old],
                                                     "findings": [{"severity": "blocker", "text": "x", "basis": ""}]}}]})
        self.go(tid)
        self.assertEqual([e.id for e in journal.open_findings(self.board, tid)], [old])   # not closed by a failed run
        self.assertEqual(len(self.entries(tid, "FINDING")), 1)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "ask", "summary": "s", "question": "q?",
                                                     "findings": [{"severity": "major", "text": "dup", "basis": ""}]}}]})
        self.go(tid)
        self.assertEqual(len(self.entries(tid, "FINDING")), 1)   # no duplicate findings from an ask

    def test_long_closed_id_does_not_break_recording(self):
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok", "closed": ["#" + "9" * 450]}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")
        (fail,) = self.entries(tid, "FAIL")
        self.assertLessEqual(journal.utf8_len(fail.text), journal.TEXT_LIMIT)

    def test_duplicate_closed_ids_are_closed_once(self):
        tid = self.task_in("reviewing", round=2, with_commit=True)
        f = journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="old",
                                basis="", author="agent:review")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok", "closed": [f, f"#{f}"]}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")
        self.assertEqual([e.status for e in self.entries(tid, "FINDING")], ["open", "closed"])
        self.assertEqual(self.entries(tid, "FAIL"), [])

    def test_task_that_left_the_stage_is_not_touched(self):
        # a PD-30 reject while the job ran: nothing is recorded and running is cleared
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        run = stages.run_stage(self.repo, self.board, self.cfg, plan, 1, on_start=noop, on_rate_limit=noop,
                               stop_event=threading.Event())
        self.board.set_running(tid, kind="agent", pid=1, group=1, fingerprint=None, slot=1)
        self.board.update_task(tid, expect_state="writing", expect_version=self.task(tid).version, state="rejected",
                               rejected_by="human")
        self.assertIsNone(stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, run))
        self.assertEqual(self.entries(tid, "VERDICT"), [])
        self.assertFalse(self.task(tid).running)

    def test_running_is_cleared_on_every_path(self):
        for actions, label in (([*self.commit_action(), {"result": READY}], "applied"),
                               ([{"result_error": 429}], "requeue")):
            tid = self.task_in("writing")
            self.scenario({f"{tid}:write": actions})
            self.board.set_running(tid, kind="agent", pid=1, group=1, fingerprint=None, slot=1)
            self.go(tid)
            self.assertFalse(self.task(tid).running, label)
        tid = self.task_in("writing")
        self.board.set_running(tid, kind="agent", pid=1, group=1, fingerprint=None, slot=1)
        stages.finish_internal_error(self.repo, self.board, self.cfg, tid, "boom")
        self.assertFalse(self.task(tid).running)

    def test_running_is_cleared_when_the_transition_raises(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        self.board.set_running(tid, kind="agent", pid=1, group=1, fingerprint=None, slot=1)
        with mock.patch.object(stages.transitions, "apply", side_effect=stages.transitions.StaleTask("lost")):
            with self.assertRaises(stages.transitions.StaleTask):
                self.go(tid)
        self.assertFalse(self.task(tid).running)

    def test_new_commits_are_measured_on_the_task_branch(self):
        # catches: measuring slot HEAD; a writer that left the task branch and committed elsewhere
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"git": ["switch", "-c", "elsewhere"]}, *self.commit_action(),
                                        {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.new_commits)
        self.assertEqual(self.task(tid).state, "open")

    def test_on_start_kinds_and_setup_gets_the_runner_environment(self):
        out = self.tmp / "setup-env.json"
        code = ("import json, os\n"
                f"json.dump(sorted(k for k in os.environ if k.startswith('ARTEL_')), "
                f"open({json.dumps(str(out))}, 'w', encoding='utf-8'))\n")
        self.configure(**{"commands.setup": py_cmd(code)})
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        kinds = []
        self.go(tid, on_start=lambda kind, pid, group, fp: kinds.append(kind))
        self.assertEqual(kinds, ["setup", "agent"])
        seen = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("ARTEL_PROBE", seen)
        self.assertNotIn("ARTEL_TASK", seen)   # setup runs with the runner's environment, not the agent's
        self.assertIn("ARTEL_TASK", self.calls()[0]["env"])

    def test_the_agent_on_start_kind_is_agent(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "fail", "summary": "x"}}]})
        kinds = []
        self.go(tid, on_start=lambda kind, pid, group, fp: kinds.append(kind))
        self.assertEqual(kinds, ["agent"])

    def test_stopped_setup_requeues_without_starting_the_agent(self):
        self.configure(**{"commands.setup": py_cmd("import time; time.sleep(30)")})
        tid = self.task_in("writing")
        stop = threading.Event()
        stop.set()
        plan, run, tr = self.go(tid, stop=stop)
        self.assertEqual(run.setup, "stopped")
        self.assertIsNone(tr)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("writing", 0))
        self.assertEqual(self.calls(), [])

    def test_verify_log_reaches_the_fix_prompt(self):
        tid = self.task_in("fixing", fix_kind="verify", with_commit=True)
        logs = self.repo.logs_dir / str(tid)
        logs.mkdir(parents=True)
        (logs / "verify-1.log").write_text("OLD RUN FAILURE\n", encoding="utf-8")
        (logs / "verify-2.log").write_text("SECOND RUN FAILURE\n", encoding="utf-8")
        (logs / "verify-10.log").write_text("TENTH RUN FAILURE\n", encoding="utf-8")
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "fail", "summary": "x"}}]})
        plan, run, tr = self.go(tid)
        self.assertIn("TENTH RUN FAILURE", plan.prompt)   # numeric order, not lexical
        self.assertNotIn("SECOND RUN FAILURE", plan.prompt)

    def test_fix_prompt_lists_review_findings_but_not_attack_findings(self):
        tid = self.task_in("fixing", fix_kind="review", with_commit=True)
        journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="review issue",
                            basis="", author="agent:review")
        journal.add_finding(self.board, task_id=tid, stage="attack", round=0, severity="major", text="attack issue",
                            basis="", author="agent:attack")
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "fail", "summary": "x"}}]})
        plan, run, tr = self.go(tid)
        self.assertIn("review issue", plan.prompt)
        block = plan.prompt.split('<untrusted-notes source="findings">')[1].split("</untrusted-notes>")[0]
        self.assertIn("review issue", block)
        self.assertNotIn("attack issue", block)   # not offered to the fixer as a finding to fix

    def test_closure_template_lists_the_open_findings_in_its_own_format(self):
        tid = self.task_in("reviewing", round=2, with_commit=True)
        f = journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="a bug",
                                basis="", author="agent:review")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "fail", "summary": "x"}}]})
        plan, run, tr = self.go(tid)
        self.assertIn(f"#{f} [major] a bug", plan.prompt)   # the closure_list format, not the history one

    def test_long_ask_question_is_truncated(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "ask", "summary": "s", "question": "я" * 500}}]})
        self.go(tid)
        (ask,) = self.entries(tid, "ASK")
        self.assertLessEqual(journal.utf8_len(ask.text), journal.TEXT_LIMIT)

    def test_internal_error_entry_carries_the_stage_and_names_the_cap(self):
        self.configure(max_attempts=1)
        tid = self.task_in("writing")
        stages.finish_internal_error(self.repo, self.board, self.cfg, tid, "boom")
        (fail,) = self.entries(tid, "FAIL")
        self.assertEqual(fail.stage, "write")
        verdicts = [e.text for e in self.entries(tid, "VERDICT")]
        self.assertTrue(any("max_attempts 1" in v for v in verdicts), verdicts)

    def test_a_failed_stage_names_the_attempt_budget(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "fail", "summary": "nope"}}]})
        self.go(tid)
        verdicts = [e.text for e in self.entries(tid, "VERDICT")]
        self.assertTrue(any(v.startswith("RETRY write r0") and "max_attempts 2" in v for v in verdicts), verdicts)

    def test_diff_lines_uses_the_merge_base(self):
        # catches: a two-dot diff, which would count what integration gained after the branch forked
        tid = self.task_in("reviewing", round=1)
        self.make_branch(tid, name="a.txt", content="1\n2\n3\n")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("int-only.txt", "1\n2\n3\n4\n5\n6\n7\n", "integration moved on")
        git(self.repo_path, "checkout", "-q", "main")
        self.assertEqual(stages.diff_lines(self.repo, tid), 3)


class Round2Test(StagesCase):
    """Controller review of T13 (PD-46, merge_committed, minors)."""

    def conflict_task(self):
        tid = self.task_in("fixing", fix_kind="conflict")
        self.make_branch(tid, name="shared.txt", content="mine\n")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "theirs\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        return tid

    def test_reset_branch_closes_open_review_findings_as_superseded(self):
        # PD-46: write -> review major -> fix ready without a commit -> R17 -> write -> review pass -> verifying
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "bug in deleted code", "basis": ""}]}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "fixing")
        attack = journal.add_finding(self.board, task_id=tid, stage="attack", round=0, severity="major",
                                     text="about the task", basis="", author="agent:attack")
        self.scenario({f"{tid}:fix": [{"result": READY}]})   # ready without a commit -> R17: branch reset
        self.go(tid)
        task = self.task(tid)
        self.assertEqual(task.state, "open")
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))
        self.assertEqual(journal.open_findings(self.board, tid), [])   # review findings superseded
        closing = [e for e in self.entries(tid, "FINDING") if e.status == "closed"]
        self.assertEqual([(e.text, e.author, e.basis) for e in closing],
                         [("superseded: branch reset (R17)", "runner", "superseded: branch reset (R17)")])
        self.assertEqual([e.id for e in journal.open_findings(self.board, tid, stages=("attack",))], [attack])
        # the fresh write and the fresh review go through without the stale finding
        self.board.update_task(tid, expect_state="open", expect_version=task.version, state="writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        self.go(tid)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "fine"}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")

    def test_no_supersede_without_a_reset(self):
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.configure(review_rounds=5)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "real", "basis": ""}]}}]})
        self.go(tid)
        self.assertEqual(len(journal.open_findings(self.board, tid)), 1)

    def test_reset_hard_to_integration_is_not_a_committed_merge(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"git": ["merge", "--abort"]}, {"git": ["reset", "--hard", INTEGRATION]},
                                     {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)   # the merged commit is in the branch but the work was dropped
        self.assertEqual(self.task(tid).state, "open")

    def test_denials_of_a_requeued_run_are_recorded(self):
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"denied": ["Bash", {"command": "curl evil"}]}, {"result_error": 429}]})
        plan, run, tr = self.go(tid)
        self.assertIsNone(tr)
        (fail,) = self.entries(tid, "FAIL")
        self.assertTrue(fail.text.startswith("denied: Bash "))
        self.assertEqual(fail.stage, "write")

    def test_a_lost_cas_leaves_no_entries_behind(self):
        # retry after an apply error must not duplicate FINDINGs
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "a bug", "basis": ""}]}}]})
        with mock.patch.object(stages.transitions, "apply", side_effect=stages.transitions.StaleTask("lost")):
            with self.assertRaises(stages.transitions.StaleTask):
                self.go(tid)
        self.assertEqual(self.entries(tid), [])
        self.go(tid)   # the retry records the finding once
        self.assertEqual(len(self.entries(tid, "FINDING")), 1)

    def test_blocking_review_below_the_cap_names_review_rounds(self):
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "a bug", "basis": ""}]}}]})
        self.go(tid)
        verdicts = [e.text for e in self.entries(tid, "VERDICT")]
        self.assertTrue(any(v.startswith("FIX review r1") and "review_rounds 2" in v for v in verdicts), verdicts)

    def test_attack_with_an_existing_branch_and_no_then_runs_in_the_branch(self):
        self.assertTrue(stages.branchless(make_task(then=None), "attack"))
        self.assertTrue(stages.branchless(make_task(then=None), "attack", False))
        self.assertTrue(stages.branchless(make_task(then="open"), "attack", True))
        self.assertFalse(stages.branchless(make_task(then=None), "attack", True))
        self.assertTrue(stages.branchless(make_task(), "vet", True))

    def test_prepare_failure_leaves_the_slot(self):
        tid = self.task_in("writing")
        (self.slot_dir() / "junk.txt").write_text("x", encoding="utf-8")
        real = slots.prepare_slot

        def broken(repo, board, cfg, n, task_id):
            real(repo, board, cfg, n, task_id)
            raise slots.GitError(["switch"], "boom")

        with mock.patch.object(slots, "prepare_slot", broken):
            with self.assertRaises(slots.GitError):
                stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        self.assert_slot_left()

    def test_stopped_run_row_is_killed_and_rate_limited_outcome_is_named(self):
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"sleep": 30}]})
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        self.go(tid, stop=stop)
        (row,) = self.board.list_runs(tid)
        self.assertEqual((row.outcome, row.killed, row.timed_out), ("stopped", True, False))
        self.scenario({f"{tid}:write": [{"result_error": 429}]})
        self.go(tid)
        self.assertEqual(self.board.list_runs(tid)[-1].outcome, "rate_limited")

    def test_kill_grace_and_timeout_come_from_the_config(self):
        self.configure(stage_timeout="77s", kill_grace="3s")
        tid = self.task_in("writing")
        seen = {}
        real = stages.procs.run_tree

        def spy(argv, **kw):
            seen.update(timeout=kw["timeout"], kill_grace=kw["kill_grace"])
            return real(argv, **kw)

        self.scenario({f"{tid}:write": [{"result": {"outcome": "fail", "summary": "x"}}]})
        with mock.patch.object(stages.procs, "run_tree", spy):
            self.go(tid)
        self.assertEqual(seen, {"timeout": 77.0, "kill_grace": 3.0})

    def test_multibyte_finding_basis_is_cut_on_a_character_boundary(self):
        tid = self.task_in("reviewing", round=1, with_commit=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "t", "basis": "я" * 150}]}}]})
        self.go(tid)
        (e,) = journal.open_findings(self.board, tid)
        self.assertLessEqual(journal.utf8_len(e.basis), journal.BASIS_LIMIT)
        e.basis.encode("utf-8")
        self.assertTrue(e.basis.endswith("…"))

    def test_attack_beats_risky_in_the_model_reason(self):
        self.assertEqual(stages.choose_model(self.cfg, make_task(attack=True, risky=True), "review", 0),
                         ("opus", "task --attack"))


if __name__ == "__main__":
    unittest.main()

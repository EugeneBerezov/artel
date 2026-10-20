import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from artel import agent_claude, board as B, config as C, journal, slots, stages
from artel.agent_claude import RateLimit
from artel.gitutil import INTEGRATION, Repo
from tests.factories import make_task
from tests.helpers import RepoTestCase, git, py_cmd, subprocess_env

FAKE = Path(__file__).parent / "fake_agent.py"


def noop(*args, **kwargs):
    return None


READY = {"outcome": "ready", "summary": "done"}


class StagesCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)
        self.board = B.open_board(self.repo.board_path)
        self.addCleanup(self.board.close)
        self.fake_dir = self.tmp / "fake"
        self.fake_dir.mkdir()
        self.limits: list[RateLimit] = []
        self.configure()
        self.base_env = subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir), ARTEL_PROBE="marker-42")

    def configure(self, **cli):
        cli.setdefault("agent_command", [sys.executable, str(FAKE)])
        cli.setdefault("stage_timeout", "60s")
        cli.setdefault("kill_grace", "1s")
        self.cfg = C.load_config(self.repo, env={}, cli={k: (v, "test") for k, v in cli.items()})
        slots.ensure_slots(self.repo, self.board, self.cfg, 1)

    def scenario(self, data):
        (self.fake_dir / "scenario.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")

    def calls(self):
        path = self.fake_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def task_in(self, state, *, with_commit=False, **fields):
        tid = self.board.insert_task(title="the task", state="open", created_by="human", area=fields.pop("area", ()))
        if with_commit:
            self.make_branch(tid)
        task = self.board.get_task(tid)
        if state != "open" or fields:
            self.assertTrue(self.board.update_task(tid, expect_state="open", expect_version=task.version,
                                                   state=state, **fields))
        return tid

    def make_branch(self, tid, name="work.txt", content="x\n"):
        slots.prepare_slot(self.repo, self.board, self.cfg, 1, tid)
        sha = self.commit_file(name, content, f"work {tid}", cwd=slots.slot_path(self.repo, self.cfg, 1))
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        return sha

    def go(self, tid, *, stop=None):
        task = self.board.get_task(tid)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, task, 1, base_env=self.base_env)
        run = stages.run_stage(self.repo, self.board, self.cfg, plan, 1, on_start=noop,
                               on_rate_limit=self.limits.append, stop_event=stop or threading.Event())
        tr = stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, run)
        return plan, run, tr

    def task(self, tid):
        return self.board.get_task(tid)

    def entries(self, tid, kind=None):
        found = journal.task_entries(self.board, tid)
        return [e for e in found if kind is None or e.kind == kind]

    def slot_dir(self):
        return slots.slot_path(self.repo, self.cfg, 1)

    def assert_slot_left(self):
        path = self.slot_dir()
        self.assertEqual(git(path, "branch", "--show-current"), "")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path,
                                           capture_output=True).returncode, 0)

    def commit_action(self, name="out.txt", message="the work"):
        return [{"write": [name, "content\n"]}, {"commit": message}]


class PureTest(StagesCase):
    def test_severities_match_journal(self):
        self.assertEqual(agent_claude.SEVERITIES, journal.SEVERITIES)

    def test_choose_model_rules(self):
        cfg = self.cfg
        strong, write, review = cfg["models.strong"], cfg["models.write"], cfg["models.review"]
        limit = cfg["models.strong_diff_lines"]
        cases = [
            ("attack", {}, 0, strong, "attack stage always uses models.strong"),
            ("write", {}, 9999, write, "models.write"),
            ("fix", {}, 9999, write, "models.write"),
            ("vet", {}, 9999, review, "vet uses models.review"),
            ("review", {}, 0, review, "models.review"),
            ("review", {}, limit, review, "models.review"),
            ("review", {"strong": True}, 0, strong, "task --strong"),
            ("review", {"attack": True}, 0, strong, "task --attack"),
            ("review", {"risky": True}, 0, strong, "task risky (vet)"),
            ("review", {}, limit + 1, strong, f"diff {limit + 1} lines > strong_diff_lines {limit}"),
            ("review", {"strong": True, "attack": True, "risky": True}, 0, strong, "task --strong"),
        ]
        for stage, fields, diff, model, reason in cases:
            with self.subTest(stage=stage, fields=fields, diff=diff):
                self.assertEqual(stages.choose_model(cfg, make_task(**fields), stage, diff), (model, reason))

    def test_diff_lines(self):
        tid = self.task_in("reviewing", round=1)
        self.assertEqual(stages.diff_lines(self.repo, tid), 0)   # no branch yet
        slots.prepare_slot(self.repo, self.board, self.cfg, 1, tid)
        path = self.slot_dir()
        self.commit_file("a.txt", "1\n2\n3\n", "add", cwd=path)
        self.commit_file("README", "changed\n", "edit", cwd=path)   # 1 added + 1 deleted
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        self.assertEqual(stages.diff_lines(self.repo, tid), 5)

    def test_branchless(self):
        self.assertTrue(stages.branchless(make_task(), "vet"))
        self.assertTrue(stages.branchless(make_task(then="open"), "attack"))
        self.assertTrue(stages.branchless(make_task(then=None), "attack"))   # a human --attack: no branch yet
        for stage in ("write", "review", "fix"):
            self.assertFalse(stages.branchless(make_task(), stage))

    def test_classify_pure_cases(self):
        task = make_task(state="writing")

        def run(**kw):
            base = dict(result=None, invalid_reason=None, usage={}, exit_code=0, timed_out=False, stopped=False,
                        rate_limited=False, new_commits=False, merge_committed=False, setup="skipped", denials=[],
                        session_id=None, first_context=None, last_context=None)
            base.update(kw)
            return stages.StageRun(**base)

        ok = {"outcome": "ready", "summary": "x", "question": "", "findings": [], "closed": []}
        cases = [
            ("requeue", run(stopped=True, result=ok, new_commits=True)),
            ("requeue", run(rate_limited=True)),
            ("ready", run(rate_limited=True, result=ok, new_commits=True)),   # PD-40: a valid result wins
            ("setup_failed", run(setup="failed")),
            ("setup_failed", run(setup="timeout")),
            ("stage_failed", run(timed_out=True, result=ok, new_commits=True)),
            ("stage_failed", run()),
            ("stage_failed", run(result=dict(ok, outcome="fail"))),
            ("stage_failed", run(result=ok, new_commits=False)),
            ("ready", run(result=ok, new_commits=True)),
            ("ask", run(result=dict(ok, outcome="ask", question="q?"))),
        ]
        for want, r in cases:
            with self.subTest(want=want):
                self.assertEqual(stages.classify(task, "write", r, self.board), want)


class WriteTest(StagesCase):
    def test_plan_uses_writer_profile_for_write_and_reviewer_for_review(self):
        write = self.task_in("writing")
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(write), 1, base_env=self.base_env)
        self.assertEqual((plan.profile, plan.stage, plan.round, plan.model), ("writer", "write", 0, "sonnet"))
        self.assertEqual(plan.argv[plan.argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(plan.log_path, self.repo.logs_dir / str(write) / "write-1.jsonl")
        self.assertIn("the task", plan.prompt)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        review = self.task_in("reviewing", round=1, with_commit=True)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(review), 1, base_env=self.base_env)
        self.assertEqual((plan.profile, plan.stage, plan.round), ("reviewer", "review", 1))
        self.assertEqual(plan.argv[plan.argv.index("--permission-mode") + 1], "default")
        self.assertIn(f"{INTEGRATION}...artel/{review}", plan.prompt)
        self.assertEqual(plan.prompt.count("\r"), 0)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        (run,) = [r for r in self.board.list_runs(review)]
        self.assertEqual((run.stage, run.model, run.model_reason, run.slot), ("review", "sonnet", "models.review", 1))

    def test_agent_gets_task_env_and_shim(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"note": ["FACT", "from the agent", "a.py:1"]}, *self.commit_action(),
                                        {"result": READY}]})
        plan, run, tr = self.go(tid)
        (note,) = self.entries(tid, "FACT")
        self.assertEqual((note.author, note.text, note.basis), ("agent:write", "from the agent", "a.py:1"))
        (call,) = self.calls()
        self.assertEqual((call["env"]["ARTEL_TASK"], call["env"]["ARTEL_STAGE"], call["env"]["ARTEL_ROUND"]),
                         (str(tid), "write", "0"))
        self.assertEqual(call["stage"], "write")
        self.assertEqual(call["prompt"], plan.prompt)   # the prompt went on stdin
        self.assertNotIn(plan.prompt, call["argv"])

    def test_base_env_reaches_agent_and_setup(self):
        out = self.tmp / "setup-env.txt"
        code = f"import os\nopen({json.dumps(str(out))}, 'w', encoding='utf-8').write(os.environ.get('ARTEL_PROBE', ''))\n"
        self.configure(**{"commands.setup": py_cmd(code)})
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        self.go(tid)
        self.assertEqual(out.read_text(encoding="utf-8"), "marker-42")
        self.assertEqual(self.calls()[0]["env"]["ARTEL_PROBE"], "marker-42")

    def test_write_ready_with_commit_goes_to_reviewing(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": READY}]})
        plan, run, tr = self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.round, task.attempts, task.running), ("reviewing", 1, 0, False))
        self.assertEqual(tr.event, "ready")
        (verdict,) = self.entries(tid, "VERDICT")
        self.assertTrue(verdict.text.startswith("READY write r0 model=sonnet (models.write)"))
        self.assert_slot_left()
        self.assertEqual(git(self.repo_path, "show", f"artel/{tid}:out.txt"), "content")
        self.assertTrue(run.new_commits)

    def test_ready_without_commit_fails_and_resets(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"write": ["stray.txt", "x"]}, {"result": READY}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("open", 1))   # R17: back to the queue, branch reset
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))
        (verdict,) = self.entries(tid, "VERDICT")
        self.assertIn("FAIL write", verdict.text)
        self.assertIn("ready without new commits", verdict.text)
        self.assert_slot_left()

    def test_writer_ready_after_requeue_with_earlier_commits(self):
        # PD-35 catches: comparing HEAD with the run start, which resets finished work after a stopped run
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"result": READY}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("reviewing", 0))

    def test_invalid_result_is_failure(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [*self.commit_action(), {"result": {"outcome": "pass", "summary": "x"}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual(task.attempts, 1)
        self.assertIn("not allowed for stage write", self.entries(tid, "VERDICT")[0].text)

    def test_error_result_is_failure(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result_error": True}]})
        self.go(tid)
        self.assertEqual(self.task(tid).attempts, 1)
        self.assertIn("reported an error", self.entries(tid, "VERDICT")[0].text)

    def test_no_result_is_failure(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"write": ["a.txt", "x"]}]})
        self.go(tid)
        self.assertIn("no result", self.entries(tid, "VERDICT")[0].text)
        self.assertEqual(self.task(tid).attempts, 1)

    def test_timeout_is_failure(self):
        self.configure(stage_timeout="1s", kill_grace="1s")
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"assistant": {"input_tokens": 7, "output_tokens": 2}}, {"sleep": 30}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.timed_out)
        self.assertEqual(self.task(tid).attempts, 1)
        (row,) = self.board.list_runs(tid)
        self.assertEqual((row.outcome, row.timed_out, row.killed), ("timeout", True, True))
        self.assertEqual(row.usage["input_tokens"], 7)   # partial usage of a killed run (PD-23)
        self.assertIn("timed out", self.entries(tid, "VERDICT")[0].text)
        self.assert_slot_left()

    def test_ask_blocks_and_records_question(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "ask", "summary": "s", "question": "which db?"}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.cause, task.asked_by, task.attempts), ("blocked", "ask", "write", 0))
        (ask,) = self.entries(tid, "ASK")
        self.assertEqual((ask.text, ask.author, ask.stage), ("which db?", "agent:write", "write"))

    def test_stopped_requeues_without_counter(self):
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"sleep": 30}]})
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        plan, run, tr = self.go(tid, stop=stop)
        self.assertTrue(run.stopped)
        self.assertIsNone(tr)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))
        self.assert_slot_left()
        self.assertEqual(self.entries(tid, "VERDICT"), [])

    def test_rate_limited_requeues_without_counter(self):
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"rate_limit": ["rejected", 1792464000, "five_hour", 1.0]},
                                        {"result_error": True}]})
        plan, run, tr = self.go(tid)
        self.assertIsNone(tr)
        self.assertEqual([e.status for e in self.limits], ["rejected"])
        self.assertEqual(self.task(tid).attempts, 0)

    def test_limit_error_result_requeues_without_counter(self):
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"result_error": 429}]})
        plan, run, tr = self.go(tid)
        self.assertIsNone(tr)
        self.assertTrue(run.rate_limited)
        (limit,) = self.limits
        self.assertEqual((limit.status, limit.rank, limit.resets_at), ("error 429", 2, None))   # PD-40
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("writing", 0))
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))

    def test_limit_hit_with_valid_result_is_applied(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"rate_limit": ["rejected", 1792464000, "five_hour", 1.0]},
                                        *self.commit_action(), {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(self.task(tid).state, "reviewing")
        self.assertTrue(run.rate_limited)
        self.assertEqual([e.rank for e in self.limits], [2])   # the runner still pauses through on_rate_limit

    def test_setup_failure_keeps_work_and_pauses(self):
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(3)")})
        tid = self.task_in("fixing", fix_kind="review", with_commit=True)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.board.get_task(tid), 1,
                                 base_env=dict(os.environ))
        run = stages.run_stage(self.repo, self.board, self.cfg, plan, 1, on_start=noop,
                               on_rate_limit=noop, stop_event=threading.Event())
        tr = stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, run)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts), ("fixing", 0))
        self.assertIn("pause_setup_failed", tr.effects)
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))   # committed work kept
        self.assertEqual(self.calls(), [])                          # agent never started
        self.assertEqual(run.setup, "failed")
        self.assert_slot_left()
        self.assertEqual(self.board.list_runs(tid)[0].outcome, "setup_failed")

    def test_permission_denial_recorded_as_fail_entry(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"denied": ["Bash", {"command": "rm -rf /"}]}, *self.commit_action(),
                                        {"result": READY}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "reviewing")   # denials do not change the outcome (PD-24)
        (fail,) = self.entries(tid, "FAIL")
        self.assertEqual(fail.author, "runner")
        self.assertTrue(fail.text.startswith("denied: Bash "))
        self.assertIn("rm -rf", fail.text)

    def test_usage_and_measurements_stored(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [
            {"assistant": {"input_tokens": 5, "output_tokens": 1, "cache_creation_input_tokens": 100,
                           "cache_read_input_tokens": 0}},
            {"assistant": {"input_tokens": 3, "output_tokens": 2, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 150}},
            *self.commit_action(), {"result": READY}]})
        plan, run, tr = self.go(tid)
        (row,) = self.board.list_runs(tid)
        self.assertEqual(row.usage, {"input_tokens": 8, "output_tokens": 3, "cache_creation_input_tokens": 100,
                                     "cache_read_input_tokens": 150})   # summed (Q6)
        self.assertEqual((row.first_context, row.last_context), (105, 153))
        self.assertEqual((row.turns, row.exit_code, row.outcome), (2, 0, "ready"))
        self.assertEqual(row.session_id, "00000000-0000-4000-8000-000000000000")
        self.assertEqual(row.cost_usd, 0.001)
        self.assertIsNotNone(row.ended_at)
        self.assertEqual((run.first_context, run.last_context), (105, 153))

    def test_leave_slot_runs_even_when_agent_crashes(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"write": ["half.txt", "x"]}, {"exit": 9}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(run.exit_code, 9)
        self.assert_slot_left()
        self.assertEqual(self.task(tid).attempts, 1)

    def test_internal_error_counts_attempt_and_records_tail(self):
        tid = self.task_in("writing")
        tr = stages.finish_internal_error(self.repo, self.board, self.cfg, tid, "Traceback ...\nValueError: boom " + "x" * 600)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("writing", 1, False))
        self.assertIn("notify_internal_error", tr.effects)
        (fail,) = self.entries(tid, "FAIL")
        self.assertTrue(fail.text.startswith("internal error: Traceback"))
        self.assertLessEqual(len(fail.text.encode("utf-8")), journal.TEXT_LIMIT)
        self.assertEqual(fail.author, "runner")

    def test_internal_error_sticks_at_the_cap(self):
        self.configure(max_attempts=1)
        tid = self.task_in("writing")
        tr = stages.finish_internal_error(self.repo, self.board, self.cfg, tid, "boom")
        self.assertEqual(self.task(tid).state, "stuck")


class FixTest(StagesCase):
    def test_fix_ready_after_answer_counts_commits_since_fix_base(self):
        tid = self.task_in("fixing", fix_kind="review", with_commit=True)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base)
        self.scenario({f"{tid}:fix": [*self.commit_action("fixed.txt", "the fix"),
                                      {"result": {"outcome": "ask", "summary": "s", "question": "q?"}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "blocked")
        task = self.task(tid)
        self.board.update_task(tid, expect_state="blocked", expect_version=task.version, state="fixing",
                               answered=True, cause=None, asked_by=None)
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "reviewing")   # the fix commit predates this run but follows fix_base

    def test_fix_ready_with_no_commit_since_fix_base_fails(self):
        tid = self.task_in("fixing", fix_kind="review", with_commit=True)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base)
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("open", 1))   # R17 also covers a failed fixer
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))

    def conflict_task(self):
        tid = self.task_in("fixing", fix_kind="conflict", with_commit=False)
        self.make_branch(tid, name="shared.txt", content="mine\n")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "theirs\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        return tid

    def test_conflict_fix_starts_with_markers_and_commit_goes_to_closure_review(self):
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(7)")})   # would fail on the markers
        tid = self.conflict_task()
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, round=1)
        self.scenario({f"{tid}:fix": [{"read": ["shared.txt", "seen.txt"]}, {"write": ["shared.txt", "merged\n"]},
                                     {"commit": "resolve conflict"}, {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertIn("<<<<<<<", (self.fake_dir / "seen.txt").read_text(encoding="utf-8"))
        self.assertEqual(plan.conflict_paths, ("shared.txt",))
        self.assertIn("shared.txt", plan.prompt)
        task = self.task(tid)
        self.assertEqual((task.state, task.review_closure, task.round), ("reviewing", True, 1))
        self.assertTrue(run.merge_committed)
        self.assertEqual(run.setup, "skipped")   # PD-43
        self.assert_slot_left()

    def test_conflict_fix_without_merge_commit_fails(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("open", 1))
        self.assertIn("ready without a merge commit", self.entries(tid, "VERDICT")[0].text)
        self.assert_slot_left()

    def test_conflict_fix_skips_setup(self):
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(7)")})
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "fail", "summary": "gave up"}}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(run.setup, "skipped")
        self.assertEqual(len(self.calls()), 1)   # the agent was started despite the failing setup

    def test_leave_slot_clears_a_merge_when_a_conflict_fixer_crashes(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"exit": 9}]})
        plan, run, tr = self.go(tid)
        self.assert_slot_left()


class ReviewTest(StagesCase):
    def review_task(self, **fields):
        fields.setdefault("round", 1)
        return self.task_in("reviewing", with_commit=True, **fields)

    def finding(self, severity="major", text="a bug", basis="work.txt:1"):
        return {"severity": severity, "text": text, "basis": basis}

    def test_review_pass_goes_to_verifying(self):
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "looks fine"}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")
        (verdict,) = self.entries(tid, "VERDICT")
        self.assertTrue(verdict.text.startswith("PASS review r1 model=sonnet (models.review)"))
        self.assertIn("looks fine", verdict.text)
        self.assert_slot_left()

    def test_review_findings_then_closure_round(self):
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s",
                                                     "findings": [self.finding("major"), self.finding("minor", "nit")]}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.fix_kind, task.round), ("fixing", "review", 1))
        opened = journal.open_findings(self.board, tid)
        self.assertEqual([(e.severity, e.text, e.author, e.stage, e.round) for e in opened],
                         [("major", "a bug", "agent:review", "review", 1), ("minor", "nit", "agent:review", "review", 1)])
        # the fixer commits; the next review is a closure round that closes the major finding
        self.board.update_task(tid, expect_state="fixing", expect_version=task.version, state="reviewing", round=2)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "closed",
                                                     "closed": [f"#{opened[0].id}", opened[1].id]}}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")
        self.assertEqual(journal.open_findings(self.board, tid), [])
        self.assertIn(f"#{opened[0].id}", plan.prompt)   # the closure template lists the open findings
        self.assertEqual(self.entries(tid, "FINDING")[-1].status, "closed")
        self.assertEqual(self.entries(tid, "FINDING")[-1].round, 2)

    def test_review_pass_without_closing_earlier_finding_blocks(self):
        # PD-3
        self.configure(review_rounds=3)
        tid = self.review_task()
        old = journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="old",
                                  basis="", author="agent:review")
        self.board.update_task(tid, expect_state="reviewing", expect_version=self.task(tid).version, round=2)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "fine"}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual(task.state, "fixing")
        self.assertEqual([e.id for e in journal.open_findings(self.board, tid)], [old])

    def test_closure_round_new_major_downgraded(self):
        # PD-4
        self.configure(review_rounds=3)
        tid = self.review_task(round=2)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            self.finding("major", "new topic"), self.finding("blocker", "real blocker")]}}]})
        self.go(tid)
        found = {e.text: e.severity for e in journal.open_findings(self.board, tid)}
        self.assertEqual(found, {"[downgraded: closure round] new topic": "minor", "real blocker": "blocker"})
        self.assertEqual(self.task(tid).state, "fixing")   # the blocker still blocks

    def test_closure_round_only_downgraded_findings_do_not_block(self):
        tid = self.review_task(round=2)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s",
                                                     "findings": [self.finding("major", "new topic")]}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")

    def test_review_closure_flag_makes_a_closure_round_at_round_one(self):
        tid = self.review_task(round=1, review_closure=True)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s",
                                                     "findings": [self.finding("major", "late")]}}]})
        self.go(tid)
        self.assertEqual([e.severity for e in journal.open_findings(self.board, tid)], ["minor"])   # PD-29 + PD-4

    def test_long_finding_text_is_truncated(self):
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            self.finding("major", "я" * 500, "b" * 500)]}}]})
        self.go(tid)
        (e,) = journal.open_findings(self.board, tid)
        self.assertLessEqual(journal.utf8_len(e.text), journal.TEXT_LIMIT)
        self.assertTrue(e.text.endswith("…"))
        self.assertLessEqual(journal.utf8_len(e.basis), journal.BASIS_LIMIT)   # PD-21

    def test_unknown_closed_id_dropped_with_fail_entry(self):
        # PD-42
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok", "closed": [999]}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("verifying", 0))
        (fail,) = self.entries(tid, "FAIL")
        self.assertEqual((fail.text, fail.author), (f"closed #999 ignored: not an open finding of #{tid}", "runner"))
        self.assertEqual([e for e in self.entries(tid, "FINDING") if e.status == "closed"], [])

    def test_attack_finding_cannot_be_closed_by_review(self):
        tid = self.review_task()
        attack = journal.add_finding(self.board, task_id=tid, stage="attack", round=0, severity="blocker",
                                     text="attack", basis="", author="agent:attack")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok", "closed": [attack]}}]})
        self.go(tid)
        self.assertEqual([e.status for e in self.entries(tid, "FINDING")], ["open"])
        self.assertEqual(len(self.entries(tid, "FAIL")), 1)
        self.assertEqual(self.task(tid).state, "verifying")   # attack findings are outside review accounting (PD-5)

    def test_review_cap_blocks_with_ask_listing_findings(self):
        self.configure(review_rounds=1)
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s",
                                                     "findings": [self.finding("major", "still broken")]}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.cause, task.asked_by), ("blocked", "cap", "review"))
        asks = self.entries(tid, "ASK")
        self.assertEqual(len(asks), 1)
        self.assertIn("still broken", asks[0].text)
        self.assertEqual(asks[0].author, "runner")
        verdicts = [e.text for e in self.entries(tid, "VERDICT")]
        self.assertTrue(any("review_rounds 1" in v for v in verdicts), verdicts)   # the cap names its setting

    def test_reviewer_uses_strong_model_for_big_diff_with_reason_in_run_row(self):
        self.configure(**{"models.strong_diff_lines": 2})
        tid = self.task_in("reviewing", round=1)
        self.make_branch(tid, name="big.txt", content="1\n2\n3\n4\n")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok"}}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(plan.model, "opus")
        (row,) = self.board.list_runs(tid)
        self.assertEqual((row.model, row.model_reason), ("opus", "diff 4 lines > strong_diff_lines 2"))
        self.assertEqual(self.calls()[0]["model"], "opus")
        self.assertIn("(diff 4 lines > strong_diff_lines 2)", self.entries(tid, "VERDICT")[0].text)


class AttackTest(StagesCase):
    def test_attack_blocker_blocks_and_clear_goes_to_writing(self):
        tid = self.task_in("attacking", attack=True)
        self.scenario({f"{tid}:attack": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "blocker", "text": "contradiction", "basis": ""}]}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.cause, task.asked_by), ("blocked", "ask", "attack"))
        task = self.task(tid)
        self.board.update_task(tid, expect_state="blocked", expect_version=task.version, state="attacking",
                               answered=True, cause=None, asked_by=None)
        self.scenario({f"{tid}:attack": [{"result": {"outcome": "pass", "summary": "clear"}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "writing")

    def test_attack_findings_not_in_review_accounting(self):
        # PD-5
        tid = self.task_in("attacking", attack=True)
        self.scenario({f"{tid}:attack": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "worth a look", "basis": ""}]}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "writing")   # only blockers block the attack stage
        self.assertEqual(journal.open_findings(self.board, tid), [])
        (found,) = journal.open_findings(self.board, tid, stages=("attack",))
        self.assertEqual((found.stage, found.severity, found.author), ("attack", "major", "agent:attack"))

    def test_writer_prompt_carries_the_attack_findings(self):
        tid = self.task_in("writing")
        journal.add_finding(self.board, task_id=tid, stage="attack", round=0, severity="major",
                            text="beware of the cache", basis="", author="agent:attack")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "fail", "summary": "x"}}]})
        plan, run, tr = self.go(tid)
        self.assertIn("beware of the cache", plan.prompt)

    def test_branchless_attack_runs_detached_at_integration(self):
        tid = self.task_in("attacking", attack=True, then="awaiting_approval")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        tip = self.commit_file("int.txt", "i\n", "integration moved")
        git(self.repo_path, "checkout", "-q", "main")
        self.scenario({f"{tid}:attack": [{"read": ["int.txt", "seen.txt"]},
                                        {"result": {"outcome": "pass", "summary": "clear"}}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(plan.head_before, tip)
        self.assertEqual((self.fake_dir / "seen.txt").read_text(encoding="utf-8"), "i\n")
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))   # no branch created
        self.assertEqual(self.task(tid).state, "awaiting_approval")
        self.assert_slot_left()
        self.assertEqual(plan.model, "opus")


if __name__ == "__main__":
    unittest.main()

import json
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from artel import agent_claude, board as B, config as C, journal, procs, protect, slots, stages, testguard, transitions
from artel.agent_claude import RateLimit
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION, Repo
from tests.factories import make_task
from tests.helpers import RepoTestCase, git, py_cmd, subprocess_env

FAKE = Path(__file__).parent / "fake_agent.py"


def noop(*args, **kwargs):
    return None


READY = {"outcome": "ready", "summary": "done"}
TURN = {"assistant": {"input_tokens": 1, "output_tokens": 1}}   # the model was reached: not agent_unavailable (PD-57)


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

    def go(self, tid, *, stop=None, on_start=noop):
        task = self.board.get_task(tid)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, task, 1, base_env=self.base_env)
        run = stages.run_stage(self.repo, self.board, self.cfg, plan, 1, on_start=on_start,
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

    def test_choose_model_protected_review_is_strong(self):
        """Mutation caught: escalating writers/fixers, a wrong reason, or protected winning over --strong."""
        cfg = self.cfg
        hit = [protect.Hit("CLAUDE.md", "protected_paths")]
        self.assertEqual(stages.choose_model(cfg, make_task(), "review", 0, protected=hit),
                         (cfg["models.strong"], "touches protected path CLAUDE.md (protected_paths)"))
        derived = [protect.Hit("tools/check.py", "commands.verify")]
        self.assertEqual(stages.choose_model(cfg, make_task(), "review", 0, protected=derived)[1],
                         "touches protected path tools/check.py (commands.verify)")
        self.assertEqual(stages.choose_model(cfg, make_task(strong=True), "review", 0, protected=hit)[1], "task --strong")
        self.assertEqual(stages.choose_model(cfg, make_task(risky=True), "review", 0, protected=hit)[1],
                         "task risky (vet)")
        for stage, model in (("write", cfg["models.write"]), ("fix", cfg["models.write"]),
                             ("vet", cfg["models.review"])):
            self.assertEqual(stages.choose_model(cfg, make_task(), stage, 0, protected=hit)[0], model, stage)
        self.assertEqual(stages.choose_model(cfg, make_task(), "review", 0), (cfg["models.review"], "models.review"))

    def test_closure_kind_reviews_use_review_model(self):
        """Mutation caught: a closure check escalated by --strong, risky, a protected hit or a big diff (PD-67)."""
        cfg = self.cfg
        hit = [protect.Hit("CLAUDE.md", "protected_paths")]
        for fields in ({"round": 2}, {"review_closure": 1}, {"review_closure": 2}):
            with self.subTest(fields=fields):
                task = make_task(strong=True, attack=True, risky=True, **fields)
                self.assertEqual(stages.choose_model(cfg, task, "review", 10_000, protected=hit),
                                 (cfg["models.review"], "closure check (models.review)"))

    def test_conflict_closure_review_uses_round1_range(self):
        """Mutation caught: `review_closure == 2` widened to truthy (a stale fix_base narrows a conflict check)."""
        stale = make_task(id=7, review_closure=1, fix_base="a" * 40)
        self.assertEqual(stages._diff_range(stale), f"{INTEGRATION}...artel/7")
        verify_fix = make_task(id=7, review_closure=2, fix_base="a" * 40)
        self.assertEqual(stages._diff_range(verify_fix), f"{'a' * 40}..artel/7")

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
            ("ready_no_commits", run(result=ok, new_commits=False)),
            ("ready", run(result=ok, new_commits=True)),
            ("ask", run(result=dict(ok, outcome="ask", question="q?"))),
        ]
        for want, r in cases:
            with self.subTest(want=want):
                self.assertEqual(stages.classify(task, "write", r, self.board), want)


    def test_classify_ready_without_commits_is_ready_no_commits(self):
        """Mutation caught: a write or verify fix without commits left as stage_failed (PD-53)."""
        for task in (make_task(state="writing"), make_task(state="fixing", fix_kind="verify")):
            stage = "write" if task.state == "writing" else "fix"
            with self.subTest(stage=stage):
                self.assertEqual(stages.classify(task, stage, self.plain_run(), self.board), "ready_no_commits")

    def test_classify_review_fix_without_commits_is_stage_failed(self):
        """Mutation caught: merging the outcomes back (a failed review fix is retried in place, PD-63)."""
        for kind in ("review", "conflict"):
            with self.subTest(fix_kind=kind):
                task = make_task(state="fixing", fix_kind=kind)
                self.assertEqual(stages.classify(task, "fix", self.plain_run(), self.board), "stage_failed")

    def test_classify_timeout_is_stage_failed(self):
        """Mutation caught: a timed-out verify fix classified as ready_no_commits."""
        task = make_task(state="fixing", fix_kind="verify")
        self.assertEqual(stages.classify(task, "fix", self.plain_run(timed_out=True), self.board), "stage_failed")
        self.assertEqual(stages.classify(task, "fix", self.plain_run(result=None), self.board), "stage_failed")

    def plain_run(self, **kw):
        base = dict(result={"outcome": "ready", "summary": "x", "question": "", "findings": [], "closed": []},
                    invalid_reason=None, usage={}, exit_code=0, timed_out=False, stopped=False, rate_limited=False,
                    new_commits=False, merge_committed=False, setup="skipped", denials=[], session_id=None,
                    first_context=None, last_context=None)
        base.update(kw)
        return stages.StageRun(**base)


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
        self.assertEqual(plan.argv[plan.argv.index("--permission-mode") + 1], "acceptEdits")
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
        verdict = self.entries(tid, "VERDICT")[0]
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
        verdict = self.entries(tid, "VERDICT")[0]
        self.assertIn("FAIL write", verdict.text)
        self.assertIn("ready without new commits", verdict.text)
        self.assert_slot_left()

    def test_write_ready_no_commits_adds_no_ask(self):
        """Mutation caught: dropping `plan.stage == "fix"` from the ASK condition (a bogus "fixer made no change"
        question on a task that goes back to the queue)."""
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": READY}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "open")
        self.assertEqual(self.entries(tid, "ASK"), [])

    def test_write_ready_no_commits_verdict_names_max_attempts(self):
        """Mutation caught: `ready_no_commits` dropped from _cause_verdict (the retry names no setting, §10)."""
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": READY}]})
        self.go(tid)
        texts = [e.text for e in self.entries(tid, "VERDICT")]
        self.assertTrue(any(t.startswith("RETRY write r") and "max_attempts" in t for t in texts), texts)

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
        self.scenario({f"{tid}:write": [TURN, {"result_error": True}]})
        self.go(tid)
        self.assertEqual(self.task(tid).attempts, 1)
        self.assertIn("reported an error", self.entries(tid, "VERDICT")[0].text)

    def test_no_result_is_failure(self):
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [TURN, {"write": ["a.txt", "x"]}]})
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
        self.scenario({f"{tid}:write": [TURN, {"write": ["half.txt", "x"]}, {"exit": 9}]})
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
        self.assertTrue(fail.text.startswith("internal error: …"))   # the head was cut, the end (which names the error) is kept
        self.assertTrue(fail.text.endswith("x" * 100))
        self.assertLessEqual(len(fail.text.encode("utf-8")), journal.TEXT_LIMIT)
        self.assertEqual(stages._clip_tail("internal error: ", "ValueError: boom"), "internal error: ValueError: boom")
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

    def verify_fix_task(self, **fields):
        tid = self.task_in("fixing", fix_kind="verify", with_commit=True, round=1, **fields)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base)
        return tid

    def test_verify_fix_no_change_journals_ask_and_blocks(self):
        """Mutation caught: R15b without its question (the blocked notification and `answer` need an ASK)."""
        tid = self.verify_fix_task(attempts=1)
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "ready", "summary": "nothing to change"}}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.asked_by, task.cause, task.attempts), ("blocked", "verify", "ask", 1))
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))   # the branch is kept (PD-53)
        (ask,) = self.entries(tid, "ASK")
        self.assertEqual(ask.text, "fixer made no change: nothing to change")
        # not a failure (spec §3.1 Counters): a FAIL verdict would be miscounted by the breaker
        self.assertFalse(any(v.text.startswith("FAIL") for v in self.entries(tid, "VERDICT")))

    def test_verify_fix_ready_goes_reviewing(self):
        """Mutation caught: R15 left at verifying (the old assertion), or the closure marker lost."""
        tid = self.verify_fix_task(attempts=1)
        self.scenario({f"{tid}:fix": [*self.commit_action("fix.txt", "the fix"), {"result": READY}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.review_closure, task.attempts, task.fix_kind),
                         ("reviewing", 2, 0, None))
        self.assertEqual(task.round, 1)   # no round added

    def test_attempts_reset_after_success(self):
        """Mutation caught: stages overwriting the table's reset (a failed write then a good one leaves attempts 1)."""
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"result": {"outcome": "fail", "summary": "no"}}]})
        self.go(tid)
        self.assertEqual((self.task(tid).state, self.task(tid).attempts), ("open", 1))
        task = self.task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=task.version, state="writing")
        self.scenario({f"{tid}:write": [*self.commit_action("w.txt", "w"), {"result": READY}]})
        self.go(tid)
        self.assertEqual((self.task(tid).state, self.task(tid).attempts), ("reviewing", 0))

    def verify_fix_review_task(self):
        tid = self.verify_fix_task(attempts=0)
        self.make_branch(tid, name="fix.txt", content="fix\n")
        task = self.task(tid)
        self.board.update_task(tid, expect_state="fixing", expect_version=task.version, state="reviewing",
                               fix_kind=None, review_closure=2, round=1)
        log_dir = self.repo.logs_dir / str(tid)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "verify-1.log").write_text("FAILED test_x: boom-marker\n", encoding="utf-8", newline="\n")
        return tid

    def test_verify_fix_review_prompt_has_open_findings(self):
        """Mutation caught: closure_list filled only for review-closure.md (the old template == condition)."""
        tid = self.verify_fix_review_task()
        journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major", text="open-marker",
                            basis="a.py:1", author="agent:review")
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok"}}]})
        plan, _, _ = self.go(tid)
        self.assertEqual(plan.prompt.count("open-marker"), 2)   # once in the history, once as the closure list
        self.assertIn("weakens, skips, deletes or loosens", plan.prompt)   # it is the verify-fix template

    def test_verify_fix_review_prompt_has_verify_log(self):
        """Mutation caught: verify_log only for stage == "fix"."""
        tid = self.verify_fix_review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok"}}]})
        plan, _, _ = self.go(tid)
        self.assertIn("boom-marker", plan.prompt)

    def test_verify_fix_review_diff_range_is_the_fix(self):
        """Mutation caught: a full-branch diff for the verify-fix review (it must see the fix only, PD-48)."""
        tid = self.verify_fix_review_task()
        fix_base = self.task(tid).fix_base
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "ok"}}]})
        plan, _, _ = self.go(tid)
        self.assertIn(f"{fix_base}..artel/{tid}", plan.prompt)
        self.assertNotIn("artel/integration...", plan.prompt)

    def test_verify_fix_review_without_fix_base_falls_back_to_the_branch_diff(self):
        task = make_task(id=5, state="reviewing", review_closure=2, fix_base=None)
        self.assertEqual(stages._diff_range(task), f"{INTEGRATION}...artel/5")

    def test_fix_ready_with_no_commit_since_fix_base_fails(self):
        tid = self.task_in("fixing", fix_kind="review", with_commit=True)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base)
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        self.go(tid)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts, task.resets), ("fixing", 1, 0))   # R17f: retried in place
        self.assertTrue(self.repo.branch_exists(f"artel/{tid}"))                       # PD-63: the branch is kept

    def test_fix_without_commit_at_the_attempt_cap_resets_once(self):
        """Mutation caught: R17g missing from the stage path (a spent fixer retried forever on the same branch)."""
        tid = self.task_in("fixing", fix_kind="review", with_commit=True, attempts=1)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base)
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        _, _, tr = self.go(tid)
        task = self.task(tid)
        self.assertEqual((tr.rid, task.state, task.attempts, task.resets), ("R17g", "open", 0, 1))
        self.assertFalse(self.repo.branch_exists(f"artel/{tid}"))

    def test_conflict_fix_that_resets_away_its_work_is_not_a_merge(self):
        """Plan: BOTH the merged commit and the tip before the fix must be ancestors. Mutation caught: counting
        `merge --abort; reset --hard artel/integration` (the branch then equals integration) as a merge."""
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"git": ["merge", "--abort"]}, {"git": ["reset", "--hard", INTEGRATION]},
                                     {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)
        self.assertEqual(self.task(tid).state, "fixing")   # R17f: the conflict fix is retried

    def test_conflict_fix_retry_after_a_reset_is_not_a_merge(self):
        """Mutation caught: judging the retry against where THIS run started (head_before). Run 1 resets the branch
        to integration (R17f keeps it in `fixing`); run 2 does nothing, `merge` is "already up to date", and both
        ancestor checks would pass on a branch that holds none of the task's work."""
        tid = self.conflict_task()
        pre_merge = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=pre_merge)
        self.scenario({f"{tid}:fix": [{"git": ["merge", "--abort"]}, {"git": ["reset", "--hard", INTEGRATION]},
                                     {"result": READY}]})
        _, _, tr = self.go(tid)
        self.assertEqual((tr.rid, self.task(tid).state), ("R17f", "fixing"))
        self.scenario({f"{tid}:fix": [{"result": READY}]})
        _, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)
        self.assertEqual((tr.rid, self.task(tid).state), ("R17g", "open"))

    def test_conflict_fix_with_a_follow_up_commit_after_the_merge_counts(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"commit": "resolve"},
                                     {"write": ["extra.txt", "more\n"]}, {"commit": "follow-up"},
                                     {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.merge_committed)
        self.assertEqual(self.task(tid).state, "reviewing")

    def test_branch_reset_supersedes_open_review_findings_but_not_attack_ones(self):
        """PD-46. Mutation caught: leaving review findings open after R17 (the fresh review inherits findings
        about deleted code)."""
        tid = self.task_in("fixing", fix_kind="review", with_commit=True, attempts=1)
        base = git(self.repo_path, "rev-parse", f"artel/{tid}")
        self.board.update_task(tid, expect_state="fixing", expect_version=self.task(tid).version, fix_base=base,
                               round=1)
        old = journal.add_finding(self.board, task_id=tid, stage="review", round=1, severity="major",
                                  text="about the old code", basis="a.py:1", author="agent:review")
        attack = journal.add_finding(self.board, task_id=tid, stage="attack", round=0, severity="minor",
                                     text="about the task", basis="", author="agent:attack")
        self.scenario({f"{tid}:fix": [{"result": READY}]})   # no commit, attempts spent: R17g
        _, _, tr = self.go(tid)
        self.assertEqual(tr.rid, "R17g")
        self.assertEqual([e.id for e in journal.open_findings(self.board, tid, stages=("review",))], [])
        self.assertEqual([e.id for e in journal.open_findings(self.board, tid, stages=("attack",))], [attack])
        closes = [e for e in self.entries(tid, "FINDING") if e.status == "closed"]
        self.assertEqual([(e.ref, e.author, e.basis) for e in closes],
                         [(old, "artel", "superseded: branch reset (R17)")])

    def test_apply_failure_leaves_no_recorded_findings(self):
        """Mutation caught: record_stage and apply in separate transactions (a retry duplicates FINDINGs)."""
        tid = self.task_in("reviewing", with_commit=True, round=1)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "t", "basis": "b"}]}}]})
        with mock.patch.object(stages.transitions, "apply", side_effect=transitions.StaleTask("lost")):
            with self.assertRaises(transitions.StaleTask):
                self.go(tid)
        self.assertEqual([e for e in self.entries(tid, "FINDING")], [])

    def test_review_below_cap_names_review_rounds(self):
        tid = self.task_in("reviewing", with_commit=True, round=1)
        self.scenario({f"{tid}:review": [{"result": {"outcome": "findings", "summary": "s", "findings": [
            {"severity": "major", "text": "t", "basis": "b"}]}}]})
        self.go(tid)
        self.assertIn("review_rounds", " ".join(e.text for e in self.entries(tid, "VERDICT")))

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
        self.assertEqual((task.state, task.review_closure, task.round), ("reviewing", 1, 1))
        self.assertTrue(run.merge_committed)
        self.assertEqual(run.setup, "not_run")   # PD-43; not "skipped" (T37 review m3)
        self.assert_slot_left()

    def test_conflict_fix_without_merge_commit_fails(self):
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"write": ["shared.txt", "merged\n"]}, {"result": READY}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.merge_committed)
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts), ("fixing", 1))
        self.assertIn("ready without a merge commit", self.entries(tid, "VERDICT")[0].text)
        self.assert_slot_left()

    def test_conflict_fix_skips_setup(self):
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(7)")})
        tid = self.conflict_task()
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "fail", "summary": "gave up"}}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(run.setup, "not_run")
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

    def test_plan_stage_passes_protected_hits(self):
        """Mutation caught: plan_stage not passing the hits (a branch editing CLAUDE.md reviewed on the cheap model)."""
        tid = self.task_in("reviewing", round=1)
        self.make_branch(tid, "CLAUDE.md", "rules\n")
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        self.assertEqual(plan.model, self.cfg["models.strong"])
        self.assertEqual(plan.model_reason, "touches protected path CLAUDE.md (protected_paths)")
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        other = self.review_task()   # work.txt: nothing protected
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(other), 1, base_env=self.base_env)
        self.assertEqual(plan.model, self.cfg["models.review"])
        slots.leave_slot(self.repo, self.board, self.cfg, 1)

    def test_soft_and_hard_hits_escalate_round_one_review(self):
        """Mutation caught: only protected hits escalating (a changed or deleted test reviewed on the cheap
        model), a wrong reason text, protected hits losing to test hits, or a closure review escalating (it stays
        on models.review, PD-67)."""
        self.commit_file("tests/test_a.py", "x = 1\n", "base test")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        strong, cheap = self.cfg["models.strong"], self.cfg["models.review"]

        def planned(files=None, *, delete=(), **fields):
            tid = self.task_in("reviewing", round=fields.pop("round", 1), **fields)
            slots.prepare_slot(self.repo, self.board, self.cfg, 1, tid)
            path = slots.slot_path(self.repo, self.cfg, 1)
            for rel, content in (files or {}).items():
                self.commit_file(rel, content, f"work {rel}", cwd=path)
            for rel in delete:
                git(path, "rm", "-q", rel)
                git(path, "commit", "-q", "-m", f"drop {rel}")
            slots.leave_slot(self.repo, self.board, self.cfg, 1)
            plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
            slots.leave_slot(self.repo, self.board, self.cfg, 1)
            return plan.model, plan.model_reason

        self.assertEqual(planned({"tests/test_a.py": "x = 2\n"}),                       # soft: a changed line
                         (strong, "test change: tests/test_a.py (test_paths)"))
        self.assertEqual(planned(delete=["tests/test_a.py"]),                            # hard: deleted
                         (strong, "test change: tests/test_a.py (test_paths)"))
        self.assertEqual(planned({"tests/test_a.py": "x = 1\nos._exit(0)\n"}),         # hard: a marker
                         (strong, "test change: tests/test_a.py (test_markers)"))
        self.assertEqual(planned({"CLAUDE.md": "r\n", "tests/test_a.py": "x = 2\n"}),   # protected comes first
                         (strong, "touches protected path CLAUDE.md (protected_paths)"))
        self.assertEqual(planned({"tests/test_b.py": "y = 1\n"}), (cheap, "models.review"))   # an addition is free
        self.assertEqual(planned({"tests/test_a.py": "x = 2\n"}, round=2),
                         (cheap, "closure check (models.review)"))

    def test_plan_stage_scans_the_diff_once(self):
        """Mutation caught: hard and soft hits each running their own `git diff` (T38 review F)."""
        self.commit_file("tests/test_a.py", "x = 1\n", "base test")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        tid = self.task_in("reviewing", round=1)
        self.make_branch(tid, "tests/test_a.py", "x = 2\n")
        real = testguard.scan
        with mock.patch.object(testguard, "scan", side_effect=real) as scan:
            stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        self.assertEqual(scan.call_count, 1)

    def test_review_pass_goes_to_verifying(self):
        tid = self.review_task()
        self.scenario({f"{tid}:review": [{"result": {"outcome": "pass", "summary": "looks fine"}}]})
        self.go(tid)
        self.assertEqual(self.task(tid).state, "verifying")
        verdict = self.entries(tid, "VERDICT")[0]
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



class NightGuardTest(StagesCase):
    """Task 37: agent_unavailable (PD-57, PD-61), stage_runs (spec §3.1 Counters), run records (PD-71 a)."""

    def run_of(self, **kw):
        base = dict(result=None, invalid_reason=None, usage={}, exit_code=1, timed_out=False, stopped=False,
                    rate_limited=False, new_commits=False, merge_committed=False, setup="skipped", denials=[],
                    session_id=None, first_context=None, last_context=None, agent_started=True)
        base.update(kw)
        return stages.StageRun(**base)

    def test_stage_run_defaults_keep_old_constructors(self):
        # catches: new StageRun fields without defaults (every older caller breaks) or a default that counts
        run = PureTest.plain_run(self)
        self.assertEqual((run.agent_started, run.unavailable, run.signature), (False, False, None))

    def test_classify_agent_unavailable_order(self):
        # catches: unavailable checked before stopped / the PD-40 requeue, or after the result checks
        task = make_task(state="writing")
        ok = {"outcome": "ready", "summary": "x", "question": "", "findings": [], "closed": []}
        cases = [
            ("agent_unavailable", self.run_of(unavailable=True)),
            ("agent_unavailable", self.run_of(unavailable=True, result=ok, new_commits=True)),
            ("requeue", self.run_of(unavailable=True, stopped=True)),
            ("requeue", self.run_of(unavailable=True, rate_limited=True)),
            ("stage_failed", self.run_of()),
        ]
        for want, run in cases:
            with self.subTest(want=want, run=run):
                self.assertEqual(stages.classify(task, "write", run, self.board), want)

    def assert_requeued_unavailable(self, tid, run, tr, tip):
        self.assertTrue(run.agent_started)
        self.assertTrue(run.unavailable)
        self.assertEqual((tr.rid, tr.event, tr.to_state), ("R38", "agent_unavailable", "writing"))
        task = self.task(tid)
        self.assertEqual((task.state, task.attempts, task.stage_runs, task.running), ("writing", 0, 0, False))
        self.assertEqual(self.repo.rev(f"artel/{tid}"), tip)   # branch kept, not reset
        self.assertEqual(self.entries(tid, "VERDICT"), [])     # not a failure verdict (the breaker reads none)
        self.assertEqual(self.board.list_runs(tid)[-1].outcome, "unavailable")
        self.assert_slot_left()

    def test_crash_before_first_call_requeues_without_counters(self):
        # catches: a crash before any record classified stage_failed (an attempt, a reset branch), or counted in
        # stage_runs, or `running` left set
        tid = self.task_in("writing", with_commit=True)
        tip = self.repo.rev(f"artel/{tid}")
        self.scenario({f"{tid}:write": [{"crash": 3, "stderr": "error: unknown option --bogus (pid 4242)"}]})
        plan, run, tr = self.go(tid)
        self.assertEqual(run.exit_code, 3)
        self.assert_requeued_unavailable(tid, run, tr, tip)
        self.assertEqual(run.signature, "exit 3: error: unknown option --bogus (pid ####)")
        (fail,) = self.entries(tid, "FAIL")
        self.assertIn(run.signature, fail.text)

    def test_exit_after_init_without_a_turn_is_unavailable(self):
        # catches: only "no record at all" taken as unavailable (PD-57 a: no assistant record and no good result)
        tid = self.task_in("writing", with_commit=True)
        tip = self.repo.rev(f"artel/{tid}")
        self.scenario({f"{tid}:write": [{"exit": 1}]})
        plan, run, tr = self.go(tid)
        self.assert_requeued_unavailable(tid, run, tr, tip)
        self.assertEqual(run.signature, "exit 1: ")

    def test_result_529_is_unavailable(self):
        # catches: PD-57 (b) missing — an overloaded API (5xx, 529) charged to the task as a failed attempt
        for status, turn in ((529, False), (503, False), (500, True), (599, True)):
            with self.subTest(status=status, turn=turn):
                tid = self.task_in("writing", with_commit=True)
                tip = self.repo.rev(f"artel/{tid}")
                self.scenario({f"{tid}:write": [*([TURN] if turn else []), {"result_error": status}]})
                plan, run, tr = self.go(tid)
                self.assertFalse(run.rate_limited)
                self.assert_requeued_unavailable(tid, run, tr, tip)
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [TURN, {"result_error": 400}]})
        plan, run, tr = self.go(tid)   # catches: any error result taken as unavailable
        self.assertFalse(run.unavailable)
        self.assertEqual((tr.event, self.task(tid).attempts), ("stage_failed", 1))

    def test_agent_that_cannot_start_is_unavailable_without_counters(self):
        # owner 2026-10-21. catches: Popen's OSError (not found, not runnable) taken as an internal error (an attempt
        # per pick), counted in stage_runs, the branch reset, the slot left checked out, or digits kept in the
        # signature (an errno would make every failure a new streak)
        tid = self.task_in("writing", with_commit=True)
        tip = self.repo.rev(f"artel/{tid}")

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")
        fake = SimpleNamespace(Popen=refuse, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
                               TimeoutExpired=subprocess.TimeoutExpired)
        with mock.patch.object(procs, "subprocess", fake):
            plan, run, tr = self.go(tid)
        self.assertEqual((run.agent_started, run.unavailable, run.exit_code), (False, True, None))
        self.assertEqual((tr.rid, tr.event, tr.to_state), ("R38", "agent_unavailable", "writing"))
        task = self.task(tid)
        self.assertEqual((task.attempts, task.stage_runs, task.running), (0, 0, False))
        self.assertEqual(self.repo.rev(f"artel/{tid}"), tip)
        self.assertTrue(run.signature.startswith("cannot start "), run.signature)
        self.assertIn("[Errno ##] Permission denied", run.signature)
        self.assertEqual(self.board.list_runs(tid)[-1].outcome, "unavailable")
        self.assert_slot_left()

    def test_unresolvable_agent_is_unavailable_before_the_slot(self):
        # owner 2026-10-21. catches: plan_stage preparing the slot or writing a `runs` row before it finds the agent
        # command missing; unstarted_stage counted (stage_runs, attempts) or not classified agent_unavailable
        tid = self.task_in("writing", with_commit=True)
        missing = str(self.tmp / "gone" / "claude7")
        self.configure(agent_command=[missing])
        with mock.patch.object(slots, "prepare_slot", side_effect=AssertionError("slot prepared")):
            with self.assertRaises(agent_claude.CannotStartAgent) as cm:
                stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        self.assertEqual(self.board.list_runs(tid), [])
        plan, run = stages.unstarted_stage(self.task(tid), str(cm.exception))
        self.assertEqual(run.setup, "not_run")
        tr = stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, run)
        self.assertEqual((tr.rid, tr.event), ("R38", "agent_unavailable"))
        task = self.task(tid)
        self.assertEqual((task.attempts, task.stage_runs), (0, 0))
        self.assertEqual(run.signature, re.sub(r"\d", "#", f"cannot find {missing} on PATH")[:200])
        self.assertEqual(stages.cannot_start_signature("x" * 300 + " 12"), "x" * 200)

    def test_rate_limit_wins_over_unavailable(self):
        # catches: a 429 without an assistant turn classified agent_unavailable (pause kind and probes differ)
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"result_error": 429}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.rate_limited)
        self.assertFalse(run.unavailable)
        self.assertIsNone(run.signature)
        self.assertIsNone(tr)   # the PD-40 requeue, not R38
        self.assertEqual([lim.status for lim in self.limits], ["error 429"])
        self.assertEqual(self.board.list_runs(tid)[-1].outcome, "rate_limited")

    def test_stopped_is_not_unavailable(self):
        # catches: dropping `not stopped` — a stop before the first turn would pause the board agent_unavailable
        tid = self.task_in("writing", with_commit=True)
        self.scenario({f"{tid}:write": [{"sleep": 30}]})
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        plan, run, tr = self.go(tid, stop=stop)
        self.assertTrue(run.stopped)
        self.assertFalse(run.unavailable)
        self.assertIsNone(tr)
        self.assertEqual((self.task(tid).stage_runs, self.task(tid).attempts), (0, 0))

    def test_timeout_before_first_turn_is_stage_failed(self):
        # catches: dropping `not timed_out` — a hung agent (PD-57: never unavailable) would loop with no counter
        self.configure(stage_timeout="1s", kill_grace="1s")
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [{"sleep": 30}]})
        plan, run, tr = self.go(tid)
        self.assertTrue(run.timed_out)
        self.assertFalse(run.unavailable)
        self.assertEqual(tr.event, "stage_failed")
        self.assertEqual((self.task(tid).attempts, self.task(tid).stage_runs), (1, 1))

    def test_unavailable_signature_rules(self):
        # catches: digits kept (a pid makes every crash a new signature), no truncation, the first line taken even
        # when blank, or a missing stderr file raising. (The streak across two crashes, plan
        # test_signature_ignores_digits, is the runner's: tests/test_runner_night.py.)
        path = self.tmp / "agent.stderr"
        path.write_text("\n   \nfatal: pid 123 at 10:42:07\nsecond line 9\n", encoding="utf-8", newline="\n")
        self.assertEqual(stages.unavailable_signature(2, path), "exit 2: fatal: pid ### at ##:##:##")
        other = self.tmp / "other.stderr"
        other.write_text("fatal: pid 98765 at 23:01:59\n", encoding="utf-8", newline="\n")
        self.assertNotEqual(stages.unavailable_signature(2, path), stages.unavailable_signature(2, other))
        other.write_text("fatal: pid 987 at 23:01:59\n", encoding="utf-8", newline="\n")
        self.assertEqual(stages.unavailable_signature(2, path), stages.unavailable_signature(2, other))
        path.write_text("x" * 500 + "\n", encoding="utf-8", newline="\n")
        signature = stages.unavailable_signature(1, path)
        self.assertEqual(len(signature), stages.SIGNATURE_LIMIT)
        self.assertTrue(signature.startswith("exit 1: xxx"))
        self.assertEqual(stages.unavailable_signature(1, self.tmp / "missing.stderr"), "exit 1: ")

    def test_signature_only_when_unavailable(self):
        # catches: a signature computed for every run (the runner would treat it as an unavailable streak)
        tid = self.task_in("writing")
        self.scenario({f"{tid}:write": [TURN, {"exit": 4}]})
        plan, run, tr = self.go(tid)
        self.assertFalse(run.unavailable)
        self.assertIsNone(run.signature)
        self.assertEqual(tr.event, "stage_failed")

    def test_stage_runs_counts_finished_runs_only(self):
        # catches: counting at start (a requeued, stopped, unavailable or setup-failed run counted), or not
        # counting a finished run (success or failure)
        tid = self.task_in("reviewing", with_commit=True, round=1)
        self.scenario({f"{tid}:review#1": [{"sleep": 30}],                      # stopped → requeue
                       f"{tid}:review#2": [{"result_error": 429}],               # limit → requeue
                       f"{tid}:review#3": [{"crash": 1, "stderr": "boom"}],      # agent_unavailable
                       f"{tid}:review#4": [TURN, {"exit": 2}],                   # stage_failed (R13): counted
                       f"{tid}:review#5": [{"result": {"outcome": "pass", "summary": "ok"}}]})   # counted
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        self.go(tid, stop=stop)
        self.go(tid)
        self.go(tid)
        self.assertEqual((self.task(tid).stage_runs, self.task(tid).attempts), (0, 0))
        self.go(tid)
        self.assertEqual((self.task(tid).stage_runs, self.task(tid).attempts), (1, 1))
        plan, run, tr = self.go(tid)
        self.assertEqual(tr.event, "review_clear")
        self.assertEqual((self.task(tid).state, self.task(tid).stage_runs), ("verifying", 2))
        # a setup failure: the agent never started
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(3)")})
        tid2 = self.task_in("fixing", fix_kind="review", with_commit=True)
        plan, run, tr = self.go(tid2)
        self.assertEqual((run.setup, run.agent_started, tr.event), ("failed", False, "setup_failed"))
        self.assertEqual(self.task(tid2).stage_runs, 0)

    def test_stage_runs_needs_agent_started(self):
        # catches: counting by event alone — a classified run whose agent never started must not count
        tid = self.task_in("reviewing", with_commit=True, round=1)
        task = self.task(tid)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, task, 1, base_env=self.base_env)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        tr = stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, self.run_of(agent_started=False))
        self.assertEqual(tr.event, "stage_failed")
        self.assertEqual((self.task(tid).attempts, self.task(tid).stage_runs), (1, 0))
        tr = stages.finish_stage(self.repo, self.board, self.cfg, tid, plan, self.run_of())
        self.assertEqual((self.task(tid).attempts, self.task(tid).stage_runs), (2, 1))

    def test_internal_error_before_spawn_not_counted(self):
        # catches: an internal error (here: the agent process cannot be started) counted in stage_runs
        tid = self.task_in("writing")
        task = self.task(tid)
        plan = stages.plan_stage(self.repo, self.board, self.cfg, task, 1, base_env=self.base_env)
        with mock.patch.object(stages.procs, "run_tree", side_effect=ArtelError("cannot start the agent")):
            with self.assertRaises(ArtelError):
                stages.run_stage(self.repo, self.board, self.cfg, plan, 1, on_start=noop, on_rate_limit=noop,
                                 stop_event=threading.Event())
        self.assert_slot_left()
        self.assertEqual(self.calls(), [])
        tr = stages.finish_internal_error(self.repo, self.board, self.cfg, tid, "cannot start the agent")
        task = self.task(tid)
        self.assertEqual((tr.event, task.attempts, task.stage_runs), ("internal_error", 1, 0))

    def test_nochange_ask_text_is_clipped(self):
        # catches: removing _clip from the "fixer made no change" ASK — EntryTooLong inside finish_stage
        tid = FixTest.verify_fix_task(self, attempts=1)
        self.scenario({f"{tid}:fix": [{"result": {"outcome": "ready", "summary": "é" * 1000}}]})   # 2 000 bytes
        plan, run, tr = self.go(tid)
        self.assertEqual(tr.event, "ready_no_commits")
        (ask,) = self.entries(tid, "ASK")
        self.assertTrue(ask.text.startswith("fixer made no change: éé"))
        self.assertLessEqual(len(ask.text.encode("utf-8")), journal.TEXT_LIMIT)
        self.assertEqual(self.task(tid).state, "blocked")

    def test_plan_stage_records_workers(self):
        # catches: plan_stage dropping `workers` (PD-71 a: the run row records the runner's worker count)
        tid = self.task_in("writing")
        plan = stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env,
                                 workers=3)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        plan2 = stages.plan_stage(self.repo, self.board, self.cfg, self.task(tid), 1, base_env=self.base_env)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        workers = {r.id: r.workers for r in self.board.list_runs(tid)}
        self.assertEqual((workers[plan.run_id], workers[plan2.run_id]), (3, None))

if __name__ == "__main__":
    unittest.main()

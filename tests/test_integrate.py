import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from artel import board as B, config as C, integrate, pauses, platform as P, slots, transitions as T
from artel.errors import ArtelError, GitError
from artel.gitutil import INTEGRATION, Repo, task_branch
from tests.helpers import RepoTestCase, git, py_cmd

OK_CODE = "import sys; sys.exit(0)"
RED_CODE = "import sys; print('boom'); sys.exit(1)"


class IntegrateCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)
        self.board = B.open_board(self.repo.board_path)
        self.addCleanup(self.board.close)
        self.configure()

    def configure(self, **overrides):
        base = {"commands.verify": py_cmd(OK_CODE)}
        base.update(overrides)
        self.cfg = C.load_config(self.repo, env={}, cli={k: (v, "test") for k, v in base.items()})
        slots.ensure_slots(self.repo, self.board, self.cfg, 1)

    def temp_worktree(self, ref, *, new_branch=None):
        path = self.tmp / f"tmpwt-{time.monotonic_ns()}"
        args = ["worktree", "add", "-q"] + (["-b", new_branch] if new_branch else []) + [str(path), ref]
        git(self.repo_path, *args)
        return path

    def commit_on_integration(self, rel, content="i\n"):
        path = self.temp_worktree(INTEGRATION)
        sha = self.commit_file(rel, content, f"integration {rel}", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        return sha

    def task_with_commit(self, state, rel, content="t\n", **fields):
        tid = self.board.insert_task(title="t", state="open", created_by="human", **fields)
        path = self.temp_worktree(INTEGRATION, new_branch=task_branch(tid))
        self.commit_file(rel, content, f"task {rel}", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        task = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state="open", expect_version=task.version, state=state))
        return tid

    def verified_task_with_commit(self, rel):
        tid = self.task_with_commit("verified", rel)
        self.board.set_meta(f"verified:{tid}", git(self.repo_path, "rev-parse", task_branch(tid)))   # PD-45
        return tid

    def verify(self, tid, **kw):
        task = self.board.get_task(tid)
        return integrate.verify_task(self.repo, self.board, self.cfg, task, 1, on_start=kw.pop("on_start", None),
                                     stop_event=kw.pop("stop_event", None), **kw)

    def finish(self, tid, result):
        return integrate.finish_verify(self.repo, self.board, self.cfg, tid, result)

    def entries(self, kind=None):
        rows = self.board.conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
        return [r for r in rows if kind is None or r["kind"] == kind]

    def slot_clean(self):
        path = slots.slot_path(self.repo, self.cfg, 1)
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertFalse((Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))).exists())
        self.assertEqual(git(path, "branch", "--show-current"), "")   # detached


class EnsureTest(IntegrateCase):
    def test_ensure_integration_branch_idempotent(self):
        git(self.repo_path, "branch", "-D", INTEGRATION)
        sha = integrate.ensure_integration_branch(self.repo, "main")
        self.assertEqual(sha, git(self.repo_path, "rev-parse", "main"))
        self.commit_on_integration("moved.txt")
        moved = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(integrate.ensure_integration_branch(self.repo, "main"), moved)   # never reset
        git(self.repo_path, "branch", "-D", INTEGRATION)
        with self.assertRaises(ArtelError):   # a missing base is an error, not a silent empty branch
            integrate.ensure_integration_branch(self.repo, "no-such-branch")


class VerifyTest(IntegrateCase):
    def test_verify_green_merges_integration_into_task_branch(self):
        tid = self.task_with_commit("verifying", "a.txt")
        self.commit_on_integration("b.txt")
        result = self.verify(tid)
        self.assertEqual((result.kind, result.detail), ("green", "exit 0"))
        tip = git(self.repo_path, "rev-parse", task_branch(tid))
        self.assertEqual(result.commit, tip)
        self.assertEqual(len(git(self.repo_path, "show", "-s", "--format=%P", tip).split()), 2)   # a merge commit
        self.assertEqual(git(self.repo_path, "show", f"{task_branch(tid)}:a.txt"), "t")
        self.assertEqual(git(self.repo_path, "show", f"{task_branch(tid)}:b.txt"), "i")
        self.slot_clean()
        (run,) = [r for r in self.board.list_runs(tid) if r.stage == "verify"]
        self.assertEqual((run.outcome, run.exit_code, run.slot), ("ok", 0, 1))

    def test_verify_red_goes_to_fix_with_counter(self):
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual((result.kind, result.detail), ("red", "exit 1"))
        self.assertIn("boom", result.log_path.read_text(encoding="utf-8"))
        tr = self.finish(tid, result)
        task = self.board.get_task(tid)
        self.assertEqual((tr.rid, task.state, task.fix_kind, task.verify_fixes, task.last_verify),
                         ("R33", "fixing", "verify", 1, "red"))
        (fail,) = self.entries("FAIL")
        self.assertIn("verify-1.log", fail["text"])
        self.assertEqual(fail["task_id"], tid)
        self.assertIn("max_verify_fixes", self.entries("VERDICT")[0]["text"])

    def test_verify_red_records_fix_base(self):
        """PD-35: fix_base is the task-branch tip INCLUDING the verify merge."""
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.commit_on_integration("b.txt")
        result = self.verify(tid)
        self.finish(tid, result)
        merged_tip = git(self.repo_path, "rev-parse", task_branch(tid))
        self.assertEqual(self.board.get_task(tid).fix_base, merged_tip)
        self.assertNotEqual(merged_tip, git(self.repo_path, "rev-parse", f"{task_branch(tid)}^1"))

    def test_verify_red_at_cap_blocks(self):
        self.configure(**{"commands.verify": py_cmd(RED_CODE), "max_verify_fixes": 1})
        tid = self.task_with_commit("verifying", "a.txt", )
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="verifying", expect_version=task.version, verify_fixes=1)
        tr = self.finish(tid, self.verify(tid))
        task = self.board.get_task(tid)
        self.assertEqual((tr.rid, task.state, task.asked_by, task.cause), ("R34a", "blocked", "verify", "cap"))
        self.assertIn("max_verify_fixes", self.entries("VERDICT")[0]["text"])

    def test_verify_conflict_goes_to_conflict_fix(self):
        tid = self.task_with_commit("verifying", "a.txt", "task side\n")
        self.commit_on_integration("a.txt", "integration side\n")
        result = self.verify(tid)
        self.assertEqual(result.kind, "conflict")
        self.assertIn("a.txt", result.detail)
        self.slot_clean()
        tr = self.finish(tid, result)
        task = self.board.get_task(tid)
        self.assertEqual((tr.rid, task.state, task.fix_kind, task.verify_fixes), ("R32", "fixing", "conflict", 1))
        self.assertEqual(self.board.list_runs(tid), [])   # no verify command ran: no runs row

    def test_verify_conflict_at_cap_blocks(self):
        self.configure(**{"max_verify_fixes": 1})
        tid = self.task_with_commit("verifying", "a.txt", "task side\n")
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="verifying", expect_version=task.version, verify_fixes=1)
        self.commit_on_integration("a.txt", "integration side\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R34b", "blocked"))

    def test_verify_setup_failure_pauses_without_counter(self):
        self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(3)")})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.kind, "setup_failed")
        tr = self.finish(tid, result)
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.verify_fixes, task.attempts), ("verifying", 0, 0))
        self.assertEqual(tr.effects, ("pause_setup_failed",))
        self.slot_clean()

    def test_verify_timeout_counts_as_red(self):
        self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "verify_timeout": "1s",
                          "kill_grace": "1s"})
        tid = self.task_with_commit("verifying", "a.txt")
        start = time.monotonic()
        result = self.verify(tid)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(result.kind, "timeout")
        tr = self.finish(tid, result)
        task = self.board.get_task(tid)
        self.assertEqual((tr.rid, task.state, task.last_verify), ("R33", "fixing", "timeout"))
        (run,) = self.board.list_runs(tid)
        self.assertTrue(run.timed_out)
        self.assertIn("verify_timeout", self.entries("VERDICT")[0]["text"])   # §10: the setting that caused it
        self.assertEqual(len(self.entries("FAIL")), 1)                           # a timeout leaves a FAIL with the log
        self.assertIn("verify-1.log", self.entries("FAIL")[0]["text"])

    def test_verify_timeout_terminates_the_process_tree(self):
        pidfile = self.tmp / "verify.pid"
        code = (f"import os, time\nopen({json.dumps(str(pidfile))}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
                "time.sleep(30)")
        self.configure(**{"commands.verify": py_cmd(code), "verify_timeout": "1s", "kill_grace": "1s"})
        tid = self.task_with_commit("verifying", "a.txt")
        self.assertEqual(self.verify(tid).kind, "timeout")
        self.assertFalse(P.pid_alive(int(pidfile.read_text(encoding="utf-8"))))

    def test_verify_env_reaches_setup_and_verify(self):
        """Mutation caught: env=None for the setup or the verify command."""
        probe = "import os, sys; sys.exit(0 if os.environ.get('ARTEL_PROBE') == '1' else 5)"
        self.configure(**{"commands.setup": py_cmd(probe), "commands.verify": py_cmd(probe)})
        tid = self.task_with_commit("verifying", "a.txt")
        env = dict(os.environ, ARTEL_PROBE="1")
        self.assertEqual(self.verify(tid, env=env).kind, "green")
        self.configure(**{"commands.setup": "", "commands.verify": py_cmd(probe)})
        self.assertEqual(self.verify(tid).kind, "red")                       # without env the probe fails
        self.assertEqual(self.verify(tid, env=env).kind, "green")

    def test_verify_records_setup_and_verify_processes(self):
        """Mutation caught: dropping on_start or reporting the wrong kind (reconcile could not find the tree)."""
        self.configure(**{"commands.setup": py_cmd(OK_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        seen = []
        self.verify(tid, on_start=lambda kind, pid, group, fp: seen.append((kind, pid, group, fp)))
        self.assertEqual([k for k, *_ in seen], ["setup", "verify"])
        for _, pid, group, fp in seen:
            self.assertIsInstance(pid, int)
            self.assertEqual(group, pid)
            self.assertTrue(fp)

    def test_verify_logs_are_numbered_per_task(self):
        """Mutation caught: a constant log number (the second verify would overwrite the first)."""
        tid = self.task_with_commit("verifying", "a.txt")
        first, second = self.verify(tid), self.verify(tid)
        self.assertEqual((first.log_path.name, second.log_path.name), ("verify-1.log", "verify-2.log"))
        self.assertEqual([r.n for r in self.board.list_runs(tid) if r.stage == "verify"], [1, 2])

    def test_setup_failure_points_at_the_setup_log(self):
        self.configure(**{"commands.setup": py_cmd("print('no network'); import sys; sys.exit(3)")})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.log_path.name, "setup-1.log")
        self.assertIn("no network", result.log_path.read_text(encoding="utf-8"))
        self.finish(tid, result)
        self.assertIn("setup-1.log", self.entries("VERDICT")[0]["basis"])

    def test_finish_verify_on_a_task_in_the_wrong_state_writes_nothing(self):
        """Mutation caught: last_verify written before the transition is checked."""
        tid = self.verified_task_with_commit("a.txt")
        before = self.board.get_task(tid)
        with self.assertRaises(T.TransitionError):
            self.finish(tid, integrate.VerifyResult("green", "abc", self.tmp / "x.log", "exit 0"))
        after = self.board.get_task(tid)
        self.assertEqual((after.version, after.last_verify), (before.version, before.last_verify))
        self.assertEqual(self.entries(), [])

    def test_verify_that_cannot_start_pauses_instead_of_burning_fixes(self):
        """Mutation caught: an environment error (missing lock/verify binary) counted as a red tree."""
        for override in ({"commands.heavy_lock": ["no-such-lock-binary-xyz", "x"]},
                         {"commands.verify": ["no-such-verify-binary-xyz"]}):
            with self.subTest(override=override):
                self.configure(**override)
                tid = self.task_with_commit("verifying", "a.txt")
                result = self.verify(tid)
                self.assertEqual(result.kind, "setup_failed")
                self.assertIn("could not start", result.detail)
                tr = self.finish(tid, result)
                task = self.board.get_task(tid)
                self.assertEqual((task.state, task.verify_fixes, task.attempts), ("verifying", 0, 0))
                self.assertEqual(tr.effects, ("pause_setup_failed",))
                self.assertEqual(self.entries("FAIL"), [])
                self.slot_clean()

    def test_verify_refuses_a_missing_task_branch(self):
        """Mutation caught: prepare_slot re-creating artel/<id> empty, then 'green' and integrated as done."""
        tid = self.task_with_commit("verifying", "a.txt")
        git(self.repo_path, "branch", "-D", task_branch(tid))
        with self.assertRaises(ArtelError) as cm:
            self.verify(tid)
        self.assertIn(task_branch(tid), str(cm.exception))
        self.assertFalse(self.repo.branch_exists(task_branch(tid)))   # nothing was re-created

    def test_merge_failure_without_conflict_raises_and_leaves_the_slot_clean(self):
        """Unrelated histories: git refuses (rc 128), no conflict files; leave_slot still runs."""
        tid = self.board.insert_task(title="t", state="verifying", created_by="human")
        tree = git(self.repo_path, "rev-parse", "main^{tree}")
        root = git(self.repo_path, "commit-tree", tree, "-m", "unrelated root")
        git(self.repo_path, "branch", task_branch(tid), root)
        with self.assertRaises(GitError):
            self.verify(tid)
        self.slot_clean()

    def test_verify_stopped_requeues_without_a_transition(self):
        stop = threading.Event()
        self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "kill_grace": "1s"})
        tid = self.task_with_commit("verifying", "a.txt")
        self.board.set_running(tid, kind="verify", pid=1, group=1, fingerprint=None, slot=1)
        threading.Timer(1.0, stop.set).start()
        result = self.verify(tid, stop_event=stop)
        self.assertEqual(result.kind, "stopped")
        self.assertIsNone(self.finish(tid, result))
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.running, task.verify_fixes), ("verifying", False, 0))

    def test_verify_runs_under_heavy_prefix_not_nested(self):
        marker = self.tmp / "lock-calls.txt"
        lock = self.tmp / "lock.py"
        lock.write_text("import subprocess, sys\n"
                        f"open({json.dumps(str(marker))}, 'a', encoding='utf-8').write('call\\n')\n"
                        "sys.exit(subprocess.call(sys.argv[1:]))\n", encoding="utf-8", newline="\n")
        self.configure(**{"commands.heavy_lock": [sys.executable, str(lock)]})
        tid = self.task_with_commit("verifying", "a.txt")
        self.assertEqual(self.verify(tid).kind, "green")
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["call"])   # exactly one per verify

    def test_verify_rechecks_setup_inputs(self):
        counter = self.tmp / "setup-runs.txt"
        code = f"open({json.dumps(str(counter))}, 'a', encoding='utf-8').write('x')"
        self.configure(**{"commands.setup": py_cmd(code), "commands.setup_inputs": ["deps.lock"]})
        tid = self.task_with_commit("verifying", "a.txt")
        self.assertEqual(self.verify(tid).kind, "green")
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")
        self.assertEqual(self.verify(tid).kind, "green")
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")                    # inputs unchanged: skipped
        self.commit_on_integration("deps.lock", "v2\n")
        self.assertEqual(self.verify(tid).kind, "green")
        self.assertEqual(counter.read_text(encoding="utf-8"), "xx")                   # the merge changed an input

    def test_empty_verify_is_green_with_note(self):
        self.configure(**{"commands.verify": ""})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.kind, "green")
        self.assertIn("verify not configured", result.detail)
        self.finish(tid, result)
        self.assertIn("verify not configured", self.entries("VERDICT")[0]["text"])
        self.assertEqual(self.board.get_task(tid).state, "verified")

    def test_merge_ask_goes_to_awaiting_accept(self):
        self.configure(merge="ask")
        tid = self.task_with_commit("verifying", "a.txt")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31b", "awaiting_accept"))
        self.assertIn("merge=ask", self.entries("VERDICT")[0]["text"])

    def test_verify_string_command_uses_configured_shell(self):
        self.configure(**{"commands.verify": "exit 1", "shell": "auto"})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual((result.kind, result.detail), ("red", "exit 1"))

    def test_runner_merge_identity_fallback(self):
        tid = self.task_with_commit("verifying", "a.txt")
        self.commit_on_integration("b.txt")
        with mock.patch.dict(os.environ):   # restored afterwards; keep PATH/SystemRoot (Windows needs them)
            for name in [k for k in os.environ if k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))]:
                del os.environ[name]
            # no empty env values: Windows treats them as "unset", which breaks GIT_CONFIG_VALUE_n
            os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="2",
                              GIT_CONFIG_KEY_0="user.useConfigOnly", GIT_CONFIG_VALUE_0="true",
                              GIT_CONFIG_KEY_1="safe.directory", GIT_CONFIG_VALUE_1="*")   # elevated Windows runner
            self.assertEqual(slots.merge_identity_args(Repo.discover(self.repo_path))[:2], ["-c", "user.name=artel"])
            result = self.verify(tid)
        self.assertEqual(result.kind, "green")
        self.assertEqual(git(self.repo_path, "log", "-1", "--format=%an", task_branch(tid)), "artel")


class IntegrateTaskTest(IntegrateCase):
    def test_integrate_fast_forwards_and_deletes_branch(self):
        tid = self.verified_task_with_commit("a.txt")
        run = self.board.start_run(task_id=tid, stage="write", round=1, model="m", model_reason=None, slot=1,
                                   log_path="x")
        self.board.finish_run(run.id, outcome="ok", usage={"input_tokens": 1200, "output_tokens": 3400},
                              exit_code=0, timed_out=False, killed=False)
        tip = git(self.repo_path, "rev-parse", task_branch(tid))
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "done")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), tip)
        self.assertFalse(self.repo.branch_exists(task_branch(tid)))
        self.assertEqual(self.board.get_task(tid).state, "done")
        (done,) = self.entries("DONE")
        self.assertLessEqual(len(done["text"].encode("utf-8")), 400)
        self.assertTrue(done["text"].startswith(f"integrated {tip[:12]}"))
        self.assertIn("usage: write 1.2k/3.4k", done["text"])

    def test_fast_forward_impossible_reverifies(self):
        tid = self.verified_task_with_commit("a.txt")
        self.commit_on_integration("b.txt")
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "reverify")
        self.assertEqual(self.board.get_task(tid).state, "verifying")
        self.assertEqual(self.board.get_task(tid).verify_fixes, 0)

    def test_update_ref_guard_refuses_concurrent_move(self):
        tid = self.verified_task_with_commit("a.txt")
        real_rev = self.repo.rev

        def moved_rev(ref):   # integration moves between the read and update-ref
            sha = real_rev(ref)
            if ref == INTEGRATION:
                self.commit_on_integration("race.txt")
            return sha

        with mock.patch.object(self.repo, "rev", side_effect=moved_rev):
            self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "retry")
        self.assertEqual(self.board.get_task(tid).state, "verified")
        self.assertTrue(self.repo.branch_exists(task_branch(tid)))

    def test_integration_checked_out_is_held(self):
        tid = self.verified_task_with_commit("a.txt")
        held = self.temp_worktree(INTEGRATION)
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "held")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)
        self.assertEqual(self.board.get_task(tid).state, "verified")
        self.assertTrue(held.is_dir())

    def test_integrate_refuses_a_task_that_is_not_verified(self):
        tid = self.task_with_commit("verifying", "a.txt")
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        with self.assertRaises(T.TransitionError):
            integrate.integrate_task(self.repo, self.board, self.cfg, tid)
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)   # the state is checked first

    def test_commit_after_verify_is_not_integrated_but_reverified(self):
        """PD-45. Mutation caught: fast-forwarding to the current branch tip instead of the verified commit."""
        tid = self.verified_task_with_commit("a.txt")
        verified = git(self.repo_path, "rev-parse", task_branch(tid))
        path = self.temp_worktree(task_branch(tid))
        self.commit_file("late.txt", "after verify\n", "late commit", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "reverify")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)   # nothing unverified got in
        self.assertEqual(self.board.get_task(tid).state, "verifying")
        self.assertIsNone(self.board.get_meta(f"verified:{tid}"))
        self.assertNotEqual(git(self.repo_path, "rev-parse", task_branch(tid)), verified)

    def test_missing_verified_record_means_reverify(self):
        tid = self.verified_task_with_commit("a.txt")
        self.board.del_meta(f"verified:{tid}")
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "reverify")

    def test_finish_verify_green_stores_the_verified_commit_and_done_clears_it(self):
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.finish(tid, result)
        self.assertEqual(self.board.get_meta(f"verified:{tid}"), result.commit)
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "done")
        self.assertIsNone(self.board.get_meta(f"verified:{tid}"))

    def test_the_verified_record_survives_accept_and_goes_with_a_reject(self):
        self.configure(merge="ask")
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.finish(tid, result)
        T.apply(self.board, tid, "accept", T.Limits.from_cfg(self.board.get_task(tid), self.cfg))
        self.assertEqual(self.board.get_meta(f"verified:{tid}"), result.commit)
        tr = T.apply(self.board, tid, "reject", T.Limits.from_cfg(self.board.get_task(tid), self.cfg))
        slots.apply_git_effects(self.repo, self.board, self.cfg, tid, tr.effects)
        self.assertIsNone(self.board.get_meta(f"verified:{tid}"))
        self.assertFalse(self.repo.branch_exists(task_branch(tid)))

    def test_red_verify_drops_a_stale_verified_record(self):
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.board.set_meta(f"verified:{tid}", "0" * 40)
        self.finish(tid, self.verify(tid))
        self.assertIsNone(self.board.get_meta(f"verified:{tid}"))

    def test_task_branch_checked_out_elsewhere_is_held(self):
        """Only the verified commit may be integrated: somebody committing on the branch holds it back."""
        tid = self.verified_task_with_commit("a.txt")
        other = self.tmp / "human"
        git(self.repo_path, "worktree", "add", "-q", str(other), task_branch(tid))
        self.commit_file("human.txt", "unverified\n", "human edit", cwd=other)
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "held")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)
        self.assertEqual(self.board.get_task(tid).state, "verified")

    def test_green_verify_touching_protected_goes_awaiting_accept(self):
        """Mutation caught: the protected check skipped on green (a CI-config change merging on its own)."""
        tid = self.task_with_commit("verifying", ".claude/settings.json", "{}\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31b", "awaiting_accept"))
        verdict = [e["text"] for e in self.entries("VERDICT") if e["text"].startswith("verify green")][0]
        self.assertIn(".claude/settings.json", verdict)
        self.assertIn("merge=ask: touches protected path(s)", verdict)
        self.assertIn("protected_paths", verdict)

    def test_protected_awaiting_accept_event_names_path(self):
        """Mutation caught: the reason journaled apart from the transition (the notification would only say
        `merge=ask`, not why)."""
        tid = self.task_with_commit("verifying", "CLAUDE.md", "rules\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual(tr.to_state, "awaiting_accept")
        self.assertEqual(tr.cause, "merge=ask: touches protected path(s) CLAUDE.md (protected_paths)")

    def test_accepted_protected_task_goes_verified(self):
        """Mutation caught: a loop accept -> re-verify -> awaiting_accept (PD-36) for a protected path."""
        tid = self.task_with_commit("verifying", "CLAUDE.md", "rules\n")
        self.finish(tid, self.verify(tid))
        self.assertEqual(self.board.get_task(tid).state, "awaiting_accept")
        T.apply(self.board, tid, "accept", T.Limits.from_cfg(self.board.get_task(tid), self.cfg))
        self.commit_on_integration("b.txt")
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "reverify")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31a", "verified"))
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "done")

    def test_unprotected_change_auto_merges(self):
        """Mutation caught: forcing merge=ask for every task."""
        tid = self.task_with_commit("verifying", "src/app.py", "x\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31a", "verified"))
        self.assertNotIn("protected", " ".join(e["text"] for e in self.entries("VERDICT")))

    def test_protected_hit_uses_verified_sha(self):
        """Mutation caught: checking the branch tip instead of the verified commit (PD-45): a commit made after
        verify is not what was verified, and integrate refuses it anyway."""
        tid = self.task_with_commit("verifying", "src/app.py", "x\n")
        result = self.verify(tid)
        path = self.temp_worktree(task_branch(tid))
        self.commit_file("CLAUDE.md", "late\n", "late edit", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        tr = self.finish(tid, result)
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31a", "verified"))

    def test_two_sources_are_both_named(self):
        """Mutation caught: only the first hit's source named (N14: the user must know every lever)."""
        self.commit_on_integration("tools/check.py", "import sys\nsys.exit(0)\n")
        self.configure(**{"commands.verify": [sys.executable, "tools/check.py"]})
        tid = self.task_with_commit("verifying", "tools/check.py", "import sys\nsys.exit(0)  # c\n")
        path = self.temp_worktree(task_branch(tid))
        self.commit_file("CLAUDE.md", "rules\n", "claude", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        tr = self.finish(tid, self.verify(tid))
        self.assertIn("commands.verify, protected_paths", tr.cause)
        self.assertIn("CLAUDE.md, tools/check.py", tr.cause)

    def test_accepted_task_cause_does_not_claim_a_forced_ask(self):
        """Mutation caught: the forced-ask text on R31a, where the task moves to verified."""
        tid = self.task_with_commit("verifying", "CLAUDE.md", "rules\n")
        self.finish(tid, self.verify(tid))
        T.apply(self.board, tid, "accept", T.Limits.from_cfg(self.board.get_task(tid), self.cfg))
        self.commit_on_integration("b.txt")
        integrate.integrate_task(self.repo, self.board, self.cfg, tid)
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual(tr.rid, "R31a")
        self.assertNotIn("touches protected", tr.cause)

    def test_protected_hit_with_merge_ask_keeps_plain_cause(self):
        """Mutation caught: the protected reason replacing the cause of a task that was `merge = ask` anyway."""
        self.configure(merge="ask")
        tid = self.task_with_commit("verifying", "CLAUDE.md", "rules\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.to_state, tr.cause), ("awaiting_accept", "merge=ask"))

    def test_derived_verify_script_protected(self):
        """Mutation caught: a change to the list-form verify script merging on its own (source named)."""
        self.commit_on_integration("tools/check.py", "import sys\nsys.exit(0)\n")
        self.configure(**{"commands.verify": [sys.executable, "tools/check.py"]})
        tid = self.task_with_commit("verifying", "tools/check.py", "import sys\nsys.exit(0)  # changed\n")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual(self.board.get_task(tid).state, "awaiting_accept")
        self.assertIn("(commands.verify)", tr.cause)

    def test_accept_then_integration_moved_reverifies_then_done(self):
        """PD-36: merge=ask; the accept survives a re-verify; no second awaiting_accept."""
        self.configure(merge="ask")
        tid = self.task_with_commit("verifying", "a.txt")
        self.finish(tid, self.verify(tid))
        self.assertEqual(self.board.get_task(tid).state, "awaiting_accept")
        T.apply(self.board, tid, "accept", T.Limits.from_cfg(self.board.get_task(tid), self.cfg))
        self.commit_on_integration("b.txt")
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "reverify")
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31a", "verified"))
        self.assertEqual(integrate.integrate_task(self.repo, self.board, self.cfg, tid), "done")


class BaseTest(IntegrateCase):
    def commit_on_base(self, rel, content="b\n"):
        return self.commit_file(rel, content, f"base {rel}")   # the main worktree has `main` checked out

    def test_base_unchanged(self):
        self.assertFalse(integrate.base_needs_update(self.repo, self.cfg))
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "unchanged")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "noop")

    def test_base_fast_forward_when_integration_behind(self):
        new = self.commit_on_base("b.txt")
        self.assertTrue(integrate.base_needs_update(self.repo, self.cfg))
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual((bu.kind, bu.new), ("fast_forward", new))
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "applied")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), new)
        self.assertEqual(self.board.get_meta("last_base"), new)

    def test_base_merged_and_verified(self):
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "merged")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "applied")
        tip = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(tip, bu.new)
        self.assertEqual(git(self.repo_path, "show", f"{INTEGRATION}:task.txt"), "i")
        self.assertEqual(git(self.repo_path, "show", f"{INTEGRATION}:b.txt"), "b")
        self.slot_clean()

    def test_base_conflict_pauses_board(self):
        self.commit_on_integration("c.txt", "integration\n")
        self.commit_on_base("c.txt", "base\n")
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "conflict")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "paused")
        self.assertIn("base_conflict", pauses.active(self.board))
        self.assertIn("c.txt", pauses.active(self.board)["base_conflict"].detail)
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)
        self.slot_clean()

    def test_base_red_verify_pauses_board(self):
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "red")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "paused")
        self.assertTrue(pauses.active(self.board)["base_conflict"].detail.startswith("red:"))
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)

    def test_base_update_held_when_integration_is_checked_out(self):
        """Mutation caught: dropping the PD-13 check from finish_base_update."""
        new = self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        held = self.temp_worktree(INTEGRATION)
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "held")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)
        self.assertTrue(held.is_dir())
        self.assertNotEqual(before, new)

    def test_base_update_old_value_guard(self):
        """Mutation caught: update-ref without the old-value guard (a task integrated meanwhile would be lost)."""
        self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "fast_forward")
        moved = self.commit_on_integration("race.txt")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "retry")
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), moved)

    def test_base_setup_failure_pauses_with_its_log(self):
        self.configure(**{"commands.setup": py_cmd("print('setup broke'); import sys; sys.exit(4)")})
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual((bu.kind, bu.detail), ("red", "setup failed"))
        self.assertEqual(bu.log_path, self.repo.logs_dir / "base" / "setup-1.log")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "paused")
        (fail,) = self.entries("FAIL")
        self.assertIn("setup-1.log", fail["basis"])
        self.slot_clean()

    def test_base_verify_timeout_pauses_the_board(self):
        """Mutation caught: finish_base_update ignoring a timed-out base verify."""
        self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "verify_timeout": "1s",
                          "kill_grace": "1s"})
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "timeout")
        self.assertIn("verify_timeout", bu.detail)
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "paused")
        self.assertTrue(pauses.active(self.board)["base_conflict"].detail.startswith("timeout:"))
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)

    def test_base_with_empty_verify_merges(self):
        self.configure(**{"commands.verify": ""})
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "merged")
        self.assertIn("verify not configured", bu.detail)

    def test_base_update_stopped_is_a_noop(self):
        stop = threading.Event()
        self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "kill_grace": "1s"})
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        before = git(self.repo_path, "rev-parse", INTEGRATION)
        threading.Timer(1.0, stop.set).start()
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1, stop_event=stop)
        self.assertEqual(bu.kind, "stopped")
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "noop")
        self.assertEqual(pauses.active(self.board), {})
        self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), before)

    def test_last_base_is_the_merged_tip_not_the_current_one(self):
        self.commit_on_integration("task.txt")
        merged_tip = self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.commit_on_base("later.txt")   # the base moved on while the job ran
        self.assertEqual(integrate.finish_base_update(self.repo, self.board, self.cfg, bu), "applied")
        self.assertEqual(self.board.get_meta("last_base"), merged_tip)

    def test_base_verify_logs_under_base_without_run_row(self):
        """PD-34: the base job belongs to no task."""
        self.commit_on_integration("task.txt")
        self.commit_on_base("b.txt")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.log_path, self.repo.logs_dir / "base" / "verify-1.log")
        self.assertTrue(bu.log_path.is_file())
        self.assertEqual(self.board.all_runs(), [])
        integrate.finish_base_update(self.repo, self.board, self.cfg, bu)
        self.commit_on_base("c.txt")
        self.assertEqual(integrate.base_update(self.repo, self.board, self.cfg, 1).log_path,
                         self.repo.logs_dir / "base" / "verify-2.log")   # n = 1 + existing files


if __name__ == "__main__":
    unittest.main()

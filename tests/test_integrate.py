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
        self.assertEqual((run.outcome, run.exit_code, run.slot), ("green", 0, 1))

    def test_verify_red_goes_to_fix_with_counter(self):
        self.configure(**{"commands.verify": py_cmd(RED_CODE), "verify_reruns": 0})
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

    def test_git_error_forces_merge_ask(self):
        """Mutation caught: fail-open (a green verify whose protected-path check could not run merging on its own)."""
        tid = self.task_with_commit("verifying", "src/app.py", "x\n")
        good = self.verify(tid)
        broken = integrate.VerifyResult("green", "0" * 40, good.log_path, "exit 0")   # a commit git cannot diff
        tr = self.finish(tid, broken)
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31b", "awaiting_accept"))
        self.assertIn("<git error>", tr.cause)

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


class RerunTest(IntegrateCase):
    """Spec §5 step 7: a red verify runs again within the job (T36)."""

    def flaky_script(self, flag, heads):
        """Verify: red on the first run, green after; records the merged tree's HEAD on every run."""
        return py_cmd(
            "import os, subprocess, sys\n"
            f"open({json.dumps(str(heads))}, 'a', encoding='utf-8').write(\n"
            "    subprocess.check_output(['git', 'rev-parse', 'HEAD'], encoding='utf-8'))\n"
            f"first = not os.path.exists({json.dumps(str(flag))})\n"
            f"open({json.dumps(str(flag))}, 'w', encoding='utf-8').close()\n"
            "print('FAIL: test_a (tests.test_x.A.test_a)' if first else 'fine')\n"
            "sys.exit(1 if first else 0)\n")

    def lock_script(self, calls):
        script = self.tmp / "lock.py"
        script.write_text(
            "import subprocess, sys\n"
            f"log = open({json.dumps(str(calls))}, 'a', encoding='utf-8')\n"
            "log.write('acquire\\n'); log.flush()\n"
            "code = subprocess.call(sys.argv[1:])\n"
            "log.write('release\\n'); log.flush()\n"
            "sys.exit(code)\n", encoding="utf-8", newline="\n")
        return [sys.executable, str(script)]

    def mark_red_seen(self, tid):
        task = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state=task.state, expect_version=task.version,
                                               red_seen=True))

    def verify_rows(self, tid):
        return [r for r in self.board.list_runs(tid) if r.stage == "verify"]

    def test_verify_red_then_green_is_flaky_awaiting_accept(self):
        """Mutation caught: auto-merging a rerun green, rerunning setup, re-merging, holding one lock across runs."""
        flag, heads, setups, locks = (self.tmp / n for n in ("flag", "heads", "setups", "locks"))
        self.configure(**{"commands.verify": self.flaky_script(flag, heads),
                          "commands.setup": py_cmd(f"open({json.dumps(str(setups))}, 'a', encoding='utf-8').write('x')"),
                          "commands.heavy_lock": self.lock_script(locks)})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.kind, "green_flaky")
        self.assertEqual([r.outcome for r in result.runs], ["red", "green"])
        self.assertEqual([r.outcome for r in self.verify_rows(tid)], ["red", "green"])
        self.assertEqual(setups.read_text(encoding="utf-8"), "x")                       # setup once, not per run
        self.assertEqual(len(set(heads.read_text(encoding="utf-8").split())), 1)       # the same merged tree
        self.assertEqual(len(heads.read_text(encoding="utf-8").split()), 2)
        self.assertEqual(locks.read_text(encoding="utf-8").split(),
                         ["acquire", "release"] * 3)       # setup once, then one lock per verify run
        self.assertEqual(result.commit, git(self.repo_path, "rev-parse", task_branch(tid)))
        tr = self.finish(tid, result)
        task = self.board.get_task(tid)
        self.assertEqual((tr.rid, task.state, task.flaky), ("R31c", "awaiting_accept", True))
        self.assertEqual(tr.cause, "merge=ask: flaky verify (verify_reruns)")
        (fail,) = self.entries("FAIL")
        self.assertTrue(fail["text"].startswith("flaky verify: "))
        self.assertIn("verify-1.log", fail["text"])                                     # the first (red) log
        self.assertIn("failed: test_a (tests.test_x.A.test_a)", fail["text"])
        self.assertIn("flaky verify (verify_reruns)", " ".join(e["text"] for e in self.entries("VERDICT")))
        self.assertEqual(self.entries("FACT"), [])
        self.assertEqual(self.board.get_meta(f"verified:{tid}"), result.commit)         # accept can still integrate it

    def test_flaky_never_writes_fact(self):
        """Mutation caught: a FACT entry for a flaky verify (it must be a FAIL)."""
        self.configure(**{"commands.verify": self.flaky_script(self.tmp / "f", self.tmp / "h")})
        tid = self.task_with_commit("verifying", "a.txt")
        self.finish(tid, self.verify(tid))
        self.assertEqual([e["kind"] for e in self.entries() if e["kind"] in ("FACT", "FAIL")], ["FAIL"])

    def test_flaky_after_accept_still_awaits(self):
        """Mutation caught: a flaky green of an accepted task going to verified (R31a instead of R31c)."""
        self.configure(**{"commands.verify": self.flaky_script(self.tmp / "f", self.tmp / "h")})
        tid = self.task_with_commit("verifying", "a.txt")
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="verifying", expect_version=task.version, accepted=True)
        tr = self.finish(tid, self.verify(tid))
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R31c", "awaiting_accept"))

    def test_verify_reruns_zero_counts_red_at_once(self):
        """Mutation caught: a rerun although verify_reruns = 0."""
        self.configure(**{"commands.verify": self.flaky_script(self.tmp / "f", self.tmp / "h"), "verify_reruns": 0})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.kind, "red")
        self.assertEqual(len(self.verify_rows(tid)), 1)

    def test_red_twice_is_red(self):
        """Mutation caught: a second red run counted as flaky or as green."""
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        result = self.verify(tid)
        self.assertEqual(result.kind, "red")
        self.assertEqual([r.outcome for r in self.verify_rows(tid)], ["red", "red"])
        tr = self.finish(tid, result)
        self.assertEqual((tr.rid, self.board.get_task(tid).state), ("R33", "fixing"))

    def test_verify_timeout_stopped_conflict_setup_failed_not_rerun(self):
        """Mutation caught: rerunning a timeout, a stop, a conflict or a failed setup."""
        with self.subTest("timeout"):
            self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "verify_timeout": "1s",
                              "kill_grace": "1s"})
            tid = self.task_with_commit("verifying", "a.txt")
            self.assertEqual(self.verify(tid).kind, "timeout")
            self.assertEqual([r.outcome for r in self.verify_rows(tid)], ["timeout"])
        with self.subTest("stopped"):
            self.configure(**{"commands.verify": py_cmd("import time; time.sleep(30)"), "kill_grace": "1s"})
            tid = self.task_with_commit("verifying", "b.txt")
            stop = threading.Event()
            threading.Timer(1.0, stop.set).start()
            self.assertEqual(self.verify(tid, stop_event=stop).kind, "stopped")
            self.assertEqual([r.outcome for r in self.verify_rows(tid)], ["stopped"])
        with self.subTest("conflict"):
            self.configure(**{"commands.verify": py_cmd(RED_CODE)})
            tid = self.task_with_commit("verifying", "c.txt", "task\n")
            self.commit_on_integration("c.txt", "integration\n")
            self.assertEqual(self.verify(tid).kind, "conflict")
            self.assertEqual(self.verify_rows(tid), [])
        with self.subTest("setup_failed"):
            self.configure(**{"commands.setup": py_cmd("import sys; sys.exit(3)")})
            tid = self.task_with_commit("verifying", "d.txt")
            self.assertEqual(self.verify(tid).kind, "setup_failed")
            self.assertEqual(self.verify_rows(tid), [])

    def test_verify_runs_vocabulary(self):
        """Mutation caught: Task 14's ok/failed values left on verify rows."""
        self.configure(**{"commands.verify": py_cmd(OK_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.verify(tid)
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        other = self.task_with_commit("verifying", "b.txt")
        self.verify(other)
        outcomes = {r.outcome for t in (tid, other) for r in self.verify_rows(t)}
        self.assertEqual(outcomes, {"green", "red"})

    def test_verify_runs_record_queued_at(self):
        """Mutation caught: queued_at not passed to start_run for every run of the job."""
        self.configure(**{"commands.verify": py_cmd(RED_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.verify(tid, queued_at=1234.5)
        self.assertEqual([r.queued_at for r in self.verify_rows(tid)], [1234.5, 1234.5])

    def test_red_seen_task_needs_every_run_green(self):
        """Mutation caught: stopping at the first green of a task that has been red before (PD-72)."""
        self.configure(**{"commands.verify": self.flaky_script(self.tmp / "f", self.tmp / "h")})
        tid = self.task_with_commit("verifying", "a.txt")
        self.mark_red_seen(tid)
        result = self.verify(tid)
        self.assertEqual(result.kind, "green_flaky")                                    # red, green
        self.assertEqual(len(self.verify_rows(tid)), 2)

    def test_red_seen_all_green_is_green(self):
        """Mutation caught: a strict job that is never green, or one that runs only once."""
        self.configure(**{"commands.verify": py_cmd(OK_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.mark_red_seen(tid)
        result = self.verify(tid)
        self.assertEqual(result.kind, "green")
        self.assertEqual([r.outcome for r in self.verify_rows(tid)], ["green", "green"])

    def test_rerun_takes_heavy_lock_again(self):
        """Mutation caught: one lock held across the runs of a job (the second run acquires after the release)."""
        locks = self.tmp / "locks"
        self.configure(**{"commands.verify": py_cmd(RED_CODE), "commands.heavy_lock": self.lock_script(locks)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.verify(tid)
        self.assertEqual(locks.read_text(encoding="utf-8").split(), ["acquire", "release", "acquire", "release"])

    def test_verify_rerun_starts_after_previous_tree_is_gone(self):
        """Regression guard for procs.run_tree: a grandchild of run 1 is dead before run 2 starts."""
        pids, order = self.tmp / "gc.pid", self.tmp / "order"
        code = (
            "import os, subprocess, sys, time\n"
            f"order = {json.dumps(str(order))}; pids = {json.dumps(str(pids))}\n"
            "if os.path.exists(pids):\n"
            "    pid = int(open(pids, encoding='utf-8').read())\n"
            "    try:\n"
            "        os.kill(pid, 0); alive = True\n"
            "    except OSError:\n"
            "        alive = False\n"
            "    open(order, 'a', encoding='utf-8').write('alive\\n' if alive else 'gone\\n')\n"
            "    sys.exit(0)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(pids, 'w', encoding='utf-8').write(str(child.pid))\n"
            "sys.exit(1)\n")
        self.configure(**{"commands.verify": py_cmd(code), "kill_grace": "1s"})
        tid = self.task_with_commit("verifying", "a.txt")
        self.verify(tid)
        self.assertEqual(order.read_text(encoding="utf-8").split(), ["gone"])
        logs = [r.log_path for r in self.verify_rows(tid)]
        self.assertEqual(len(set(logs)), 2)

    def test_base_update_verify_never_reruns(self):
        """Mutation caught: the base-update verify rerunning a red run (spec §5 step 7)."""
        counter = self.tmp / "base-runs"
        self.configure(**{"commands.verify": py_cmd(
            f"open({json.dumps(str(counter))}, 'a', encoding='utf-8').write('x'); import sys; sys.exit(1)")})
        self.commit_on_integration("i.txt", "integration has work of its own\n")
        self.commit_file("m.txt", "m\n", "main moves")
        bu = integrate.base_update(self.repo, self.board, self.cfg, 1)
        self.assertEqual(bu.kind, "red")
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")

    def test_failed_tests_parsing(self):
        """Mutation caught: unittest/pytest failure lines not read, more than three listed, duplicates."""
        log = self.tmp / "v.log"
        log.write_text("\n".join([
            "FAIL: test_a (m.A.test_a)", "ERROR: test_b (m.A.test_b)", "FAIL: test_a (m.A.test_a)",
            "FAILED tests/t.py::test_c - AssertionError: x", "tests/t.py::test_d FAILED [ 50%]",
            "FAIL: test_e (m.A.test_e)"]) + "\r\n", encoding="utf-8", newline="")
        self.assertEqual(integrate.failed_tests(log),
                         ["test_a (m.A.test_a)", "test_b (m.A.test_b)", "tests/t.py::test_c"])
        self.assertEqual(integrate.failed_tests(log, limit=9)[3:], ["tests/t.py::test_d", "test_e (m.A.test_e)"])
        self.assertEqual(integrate.failed_tests(self.tmp / "missing.log"), [])

    def test_red_seen_green_then_red_is_flaky(self):
        """Mutation caught: the first run deciding a strict job (green first, red second must not be green)."""
        flag = self.tmp / "g"
        self.configure(**{"commands.verify": py_cmd(
            "import os, sys\n"
            f"first = not os.path.exists({json.dumps(str(flag))})\n"
            f"open({json.dumps(str(flag))}, 'w', encoding='utf-8').close()\n"
            "sys.exit(0 if first else 1)\n")})
        tid = self.task_with_commit("verifying", "a.txt")
        self.mark_red_seen(tid)
        result = self.verify(tid)
        self.assertEqual(result.kind, "green_flaky")
        self.assertEqual([r.outcome for r in result.runs], ["green", "red"])

    def test_verify_that_cannot_start_is_not_rerun(self):
        """Mutation caught: rerunning a verify command that does not start (one setup_failed row, no second try)."""
        self.configure(**{"commands.verify": ["artel-no-such-program-xyz"]})
        tid = self.task_with_commit("verifying", "a.txt")
        self.assertEqual(self.verify(tid).kind, "setup_failed")
        self.assertEqual(len(self.verify_rows(tid)), 1)

    def test_verify_rows_record_duration(self):
        """Mutation caught: duration_ms left empty on a verify row (spec §5 step 7)."""
        self.configure(**{"commands.verify": py_cmd(OK_CODE)})
        tid = self.task_with_commit("verifying", "a.txt")
        self.verify(tid)
        (row,) = self.verify_rows(tid)
        self.assertIsNotNone(row.duration_ms)

    def test_command_text_clips_by_bytes(self):
        """Mutation caught: clipping by characters (100 non-ASCII characters are 200+ bytes)."""
        text = integrate._command_text(["x" * 10, "я" * 100])
        self.assertLessEqual(len(text.encode("utf-8")), 100)
        self.assertTrue(text.endswith("…"))
        self.assertEqual(integrate._command_text(["a", "b"]), "a b")

    def test_failed_tests_skips_unittest_summary_and_ansi(self):
        """Mutation caught: 'FAILED (failures=1)' listed as a test; coloured pytest lines missed."""
        log = self.tmp / "u.log"
        log.write_text("FAIL: test_a (m.A.test_a)\nFAILED (failures=1)\n\x1b[31mFAILED\x1b[0m t.py::test_b - x\n",
                       encoding="utf-8", newline="")
        self.assertEqual(integrate.failed_tests(log), ["test_a (m.A.test_a)", "t.py::test_b"])


if __name__ == "__main__":
    unittest.main()

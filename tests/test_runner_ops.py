"""Task 22: runner operations — pauses while ticking, disk guard, stop and signals, reconcile, notifier, logs."""
import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from unittest import mock

from artel import journal, notify, pauses, platform, procs, requests, slots, stages, transitions
from artel import runner as runner_module
from artel.errors import ArtelError
from artel.events import Event
from artel.gitutil import INTEGRATION, task_branch
from artel.runner import Job
from tests.e2e import E2ECase
from tests.helpers import git, py_cmd, subprocess_env

WRITE = [{"write": ["hello.txt", "hi\n"]}, {"commit": "add hello"},
         {"result": {"outcome": "ready", "summary": "done"}}]
PASS = [{"result": {"outcome": "pass", "summary": "ok"}}]
SLEEP = [{"sleep": 30}, {"result": {"outcome": "ready", "summary": "never"}}]
GiB = 1 << 30


def sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(py_cmd(f"import time; time.sleep({seconds})"), **platform.popen_kwargs())


class OpsCase(E2ECase):
    def started(self, **kw):
        r = self.runner(**kw)
        r.start()
        return r

    def sent(self, r, kind=None) -> list[Event]:
        """Events reach the notifier on the sender thread: wait until it has handed them all over."""
        self.assertTrue(r.flush_notifications(10), "notifications not delivered")
        return [e for e in self.events if kind is None or e.kind == kind]

    def set_task(self, tid, **fields):
        task = self.board.get_task(tid)
        self.assertTrue(self.board.update_task(tid, expect_state=task.state, expect_version=task.version, **fields))

    def task_branch_with_commit(self, tid, rel="f.txt") -> str:
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        return self.commit_on(task_branch(tid), rel, f"work {tid}\n")

    def commit_on(self, branch, rel, content, message=None) -> str:
        wt = self.tmp / f"wt-{branch.replace('/', '-')}"
        git(self.repo_path, "worktree", "add", "-q", str(wt), branch)
        try:
            return self.commit_file(rel, content, message or f"{branch}: {rel}", cwd=wt)
        finally:
            git(self.repo_path, "worktree", "remove", "--force", str(wt))

    def verifying_task(self, title="v") -> int:
        tid = self.add_task(title)
        self.task_branch_with_commit(tid, f"{title}.txt")
        self.set_task(tid, state="verifying", round=1)
        return tid

    def wait_for_exit(self, proc, timeout=10):
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("process still alive")


# --- child marker (PD-65 a, b) ----------------------------------------------------------------------------------

class ChildMarkerTest(OpsCase):
    def test_children_see_artel_child(self):
        marker = self.tmp / "verify-env.txt"
        self.write_toml({"commands.verify": py_cmd(
            f"import os; open({str(marker)!r}, 'w', encoding='utf-8').write(os.environ.get('ARTEL_CHILD', '-'))")})
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=40)
        finally:
            r.shutdown()
        self.assertEqual(marker.read_text(encoding="utf-8"), "1")
        self.assertTrue(all(c["env"].get("ARTEL_CHILD") == "1" for c in self.calls()))
        self.assertNotIn("ARTEL_CHILD", os.environ)   # the runner's own environment is unchanged

    def test_run_refused_under_artel_child(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ARTEL_CHILD": "1"}), mock.patch("sys.stderr", err), \
                mock.patch.object(runner_module.Runner, "run_forever", side_effect=AssertionError("started")):
            self.assertEqual(runner_module.run_command(argparse.Namespace(n=None)), 2)
        self.assertIn("artel run is a human command; refused inside a process started by artel "
                      "(ARTEL_CHILD is set)", err.getvalue())

    def test_run_command_wires_the_dispatcher(self):
        seen = {}

        def fake_run_forever(self_):
            seen["notify"] = self_.notify
            return 0

        old = os.getcwd()
        os.chdir(self.repo_path)
        try:
            with mock.patch.object(runner_module.Runner, "run_forever", fake_run_forever):
                self.assertEqual(runner_module.run_command(argparse.Namespace(n=None)), 0)
        finally:
            os.chdir(old)
        self.assertIsInstance(getattr(seen["notify"], "__self__", None), notify.Dispatcher)


# --- reconcile (§5, PD-34) ----------------------------------------------------------------------------------------

class ReconcileTest(OpsCase):
    def test_reconcile_after_hard_kill_terminates_orphans(self):
        self.scenario({"write": [{"sleep": 60}]})
        tid = self.add_task("slow")
        proc = subprocess.Popen([sys.executable, "-m", "artel", "run", "-n", "1"], cwd=self.repo_path,
                                env=subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_until(lambda: self.board.get_task(tid).running, timeout=20)
        finally:
            proc.kill()                       # SIGKILL / TerminateProcess: the runner only
            proc.wait()
        task = self.board.get_task(tid)
        r = self.runner()
        r.start()
        try:
            self.wait_until(lambda: not platform.pid_alive(task.running_pid), timeout=10)
            task = self.board.get_task(tid)
            self.assertEqual((task.state, task.attempts, task.running), ("writing", 0, False))
        finally:
            r.shutdown()

    def test_reconcile_skips_reused_pid(self):
        tid = self.add_task("t")
        r = self.started(max_agents=1)
        proc = sleeper()
        try:
            self.set_task(tid, state="writing")
            self.board.set_running(tid, kind="agent", pid=proc.pid, group=platform.group_of(proc),
                                   fingerprint="not-this-process", slot=1)
            report = r.reconcile()
            self.assertIsNone(proc.poll())                 # a reused pid is never signalled
            self.assertEqual((report.terminated, report.requeued), ([], [tid]))
            self.assertFalse(self.board.get_task(tid).running)
            # no recorded start time: a live pid cannot be told from a reused one
            self.board.set_running(tid, kind="agent", pid=proc.pid, group=platform.group_of(proc),
                                   fingerprint=None, slot=1)
            self.assertEqual(r.reconcile().terminated, [])
            self.assertIsNone(proc.poll())
            # the same process with its real fingerprint is terminated
            self.board.set_running(tid, kind="agent", pid=proc.pid, group=platform.group_of(proc),
                                   fingerprint=platform.process_fingerprint(proc.pid), slot=1)
            report = r.reconcile()
            self.assertEqual((report.terminated, report.requeued), ([tid], [tid]))
            self.wait_for_exit(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            r.shutdown()

    def test_reconcile_terminates_orphaned_base_job(self):
        proc = sleeper()
        try:
            self.board.set_meta("base_running", json.dumps({
                "pid": proc.pid, "group": platform.group_of(proc),
                "fingerprint": platform.process_fingerprint(proc.pid), "slot": 1, "started_at": 1.0}))
            r = self.started(max_agents=1)
            r.shutdown()
            self.wait_for_exit(proc)
            self.assertIsNone(self.board.get_meta("base_running"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_heavy_lock_check_reports_stale_lock(self):
        err = io.StringIO()
        check = py_cmd("print('lock held by pid 42'); print('since 2026-10-21'); raise SystemExit(1)")
        with mock.patch("sys.stderr", err):
            r = self.started(**{"commands.heavy_lock_check": check})
            try:
                self.assertEqual(self.board.get_meta("heavy_lock_stale"), "lock held by pid 42 since 2026-10-21")
                self.assertEqual(r.reconcile().heavy_lock_stale, "lock held by pid 42 since 2026-10-21")
            finally:
                r.shutdown()
        self.assertIn("warning: the heavy lock looks stale: lock held by pid 42", err.getvalue())
        # a free lock (exit 0) clears the record
        r = self.started(**{"commands.heavy_lock_check": py_cmd("print('free')")})
        try:
            self.assertIsNone(self.board.get_meta("heavy_lock_stale"))
            self.assertIsNone(r.reconcile().heavy_lock_stale)
        finally:
            r.shutdown()

    def test_start_warns_when_review_rounds_zero(self):
        for rounds, warned in ((0, True), (1, False)):
            with self.subTest(review_rounds=rounds):
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    r = self.started(review_rounds=rounds)
                r.shutdown()
                self.assertEqual("warning: review_rounds = 0 behaves like 1" in err.getvalue(), warned)

    def test_start_after_shutdown_says_already_stopped(self):
        r = self.started()
        r.shutdown()
        with self.assertRaisesRegex(ArtelError, "runner already stopped"):
            r.start()


# --- pauses (§3.1 Pauses, PD-31, PD-54) ----------------------------------------------------------------------------

class DiskGuardTest(OpsCase):
    def setUp(self):
        super().setUp()
        self.free = 100 * GiB
        patcher = mock.patch.object(platform, "free_bytes", side_effect=lambda path: self.free)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disk_guard_pauses_and_auto_resumes(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.started(max_agents=1, min_free_disk="3G")
        try:
            self.free = 1 * GiB
            for _ in range(3):
                r.tick()
            self.assertIn("disk", pauses.active(self.board))
            self.assertEqual((r.jobs, self.board.get_task(tid).state), ([], "open"))
            self.free = 10 * GiB
            r.tick()
            self.assertNotIn("disk", pauses.active(self.board))
            self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=40)
            self.assertEqual([e.text for e in self.sent(r, "resumed")], ["disk"])
        finally:
            r.shutdown()

    def test_disk_end_keeps_manual_pause(self):
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        r = self.started(max_agents=1, min_free_disk="3G")
        try:
            self.free = 1 * GiB
            r.tick()
            self.assertEqual(set(pauses.active(self.board)), {"manual", "disk"})
            self.free = 10 * GiB
            r.tick()
            self.assertEqual(set(pauses.active(self.board)), {"manual"})
        finally:
            r.shutdown()

    def test_paused_event_text_exact(self):
        r = self.started(max_agents=1, min_free_disk="3G")
        try:
            self.free = 1 * GiB
            r.tick()
            r.tick()
            detail = pauses.active(self.board)["disk"].detail
            self.assertIn("below 3.0G", detail)
            self.assertEqual(self.sent(r, "paused"), [Event("paused", text=f"disk: {detail} (min_free_disk)")])
        finally:
            r.shutdown()

    def test_disk_resumes_only_with_a_margin(self):
        r = self.started(max_agents=1, min_free_disk="3G")
        try:
            self.free = 1 * GiB
            r.tick()
            self.free = 3 * GiB + 50 * (1 << 20)   # just above the minimum: not back yet
            r.tick()
            self.assertIn("disk", pauses.active(self.board))
            self.free = 3 * GiB + 200 * (1 << 20)  # above the minimum + 5 %
            r.tick()
            self.assertNotIn("disk", pauses.active(self.board))
            self.free = 3 * GiB - 1
            r.tick()
            self.assertIn("disk", pauses.active(self.board))
        finally:
            r.shutdown()

    def test_a_failing_disk_check_does_not_stop_the_setup_end(self):
        pauses.add(self.board, "setup_failed", "setup failed for task 1: x", now=1.0, until=2.0)
        r = self.started(max_agents=1)
        try:
            with mock.patch.object(platform, "free_bytes", side_effect=OSError("no such drive")):
                r.tick()
            self.assertNotIn("setup_failed", pauses.active(self.board))
        finally:
            r.shutdown()

    def test_both_disks_are_checked(self):
        seen = []

        def free(path):
            seen.append(path)
            return 1 * GiB if path == self.repo.root / self.cfg["slots_dir"] else 100 * GiB

        r = self.started(max_agents=1, min_free_disk="3G")
        try:
            with mock.patch.object(platform, "free_bytes", side_effect=free):
                r.tick()
            self.assertIn("disk", pauses.active(self.board))
            self.assertIn(self.repo.common_dir, seen)
        finally:
            r.shutdown()


class SetupPauseTest(OpsCase):
    def setUp(self):
        super().setUp()
        self.write_toml({"commands.setup": py_cmd("raise SystemExit(1)"), "commands.setup_inputs": ["deps.txt"],
                         "setup_retry": "15m"})
        self.scenario({"write": WRITE, "review": PASS})
        self.tid = self.add_task("Add hello")

    def failed_once(self):
        r = self.started(max_agents=1)
        self.drive(r, lambda: "setup_failed" in pauses.active(self.board) and not r.jobs)
        return r

    def test_setup_failed_pause_notifies(self):
        r = self.failed_once()
        try:
            (event,) = self.sent(r, "paused")
            self.assertTrue(event.text.startswith(f"setup_failed: setup failed for task {self.tid}: "), event.text)
            self.assertTrue(event.text.endswith("(commands.setup)"), event.text)
            self.assertEqual(event.resets_at, self.clock() + 15 * 60)
        finally:
            r.shutdown()

    def test_setup_pause_lifts_when_inputs_change(self):
        r = self.failed_once()
        try:
            for _ in range(3):
                r.tick()
            self.assertIn("setup_failed", pauses.active(self.board))   # nothing changed: it stays
            self.assertEqual(r.jobs, [])
            (slots.slot_path(self.repo, self.cfg, 1) / "deps.txt").write_text("fixed\n", encoding="utf-8")
            r.tick()
            self.assertNotIn("setup_failed", pauses.active(self.board))
            self.assertEqual([e.text for e in self.sent(r, "resumed")], ["setup_failed"])
        finally:
            r.shutdown()

    def test_setup_pause_lifts_after_setup_retry(self):
        r = self.failed_once()
        try:
            self.clock.advance(15 * 60 - 1)
            r.tick()
            self.assertIn("setup_failed", pauses.active(self.board))
            self.clock.advance(2)
            r.tick()
            self.assertNotIn("setup_failed", pauses.active(self.board))
            self.assertEqual(len([j for j in r.jobs if j.kind == "stage"]), 1)   # the retry
        finally:
            r.shutdown()

    def test_plain_resume_does_not_end_setup_failed(self):
        r = self.failed_once()
        try:
            requests.enqueue(self.repo, self.board, "resume", None, {"force": False})
            r.tick()
            self.assertIn("setup_failed", pauses.active(self.board))
        finally:
            r.shutdown()


class PausedTickTest(OpsCase):
    def test_requests_applied_while_paused(self):
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        tid = self.add_task("Ask me", area=["a"])
        self.set_task(tid, state="blocked", asked_by="write", cause="ask", round=1)
        journal.append(self.board, "ASK", "which db?", author="agent:write", task_id=tid)
        requests.enqueue(self.repo, self.board, "answer", tid, {"text": "postgres"})
        r = self.started(max_agents=1)
        try:
            r.tick()
            self.assertEqual(self.board.pending_requests(), [])
            self.assertNotEqual(self.board.get_task(tid).state, "blocked")
            self.assertEqual(r.jobs, [])                    # still paused: nothing started
        finally:
            r.shutdown()

    def test_stop_request_applied_while_paused(self):
        for via in ("request", "file"):
            with self.subTest(via=via):
                pauses.add(self.board, "manual", "artel pause", now=1.0)
                r = self.started()
                try:
                    if via == "request":
                        requests.enqueue(self.repo, self.board, "stop", None, {"now": False})
                    else:
                        self.repo.stop_path.write_text("soft", encoding="utf-8")
                    r.tick()
                    self.assertTrue(r.stopping)
                finally:
                    r.shutdown()

    def test_verify_continues_under_token_free_kind(self):
        tid = self.verifying_task()
        commit = git(self.repo_path, "rev-parse", task_branch(tid))
        pauses.add(self.board, "quota", "five_hour at 96%", now=1.0, until=self.clock() + 3600)
        other = self.add_task("waits")
        r = self.started(max_agents=2)
        try:
            self.drive(r, lambda: self.board.get_task(tid).state == "done")
        finally:
            r.shutdown()
        self.assertTrue(self.repo.is_ancestor(commit, INTEGRATION))
        self.assertEqual(self.board.get_task(other).state, "open")   # no agent stage under quota
        self.assertEqual(self.calls(), [])
        self.assertIn("quota", pauses.active(self.board))

    def test_verify_waits_under_manual_pause(self):
        tid = self.verifying_task("v")
        done = self.verifying_task("w")
        self.set_task(done, state="verified")
        self.board.set_meta(f"verified:{done}", git(self.repo_path, "rev-parse", task_branch(done)))
        tip = git(self.repo_path, "rev-parse", INTEGRATION)
        for kind in ("manual", "disk"):
            with self.subTest(kind=kind):
                pauses.add(self.board, kind, "held", now=1.0)
                with mock.patch.object(platform, "free_bytes", return_value=100 * GiB) if kind == "manual" \
                        else mock.patch.object(platform, "free_bytes", return_value=1):
                    r = self.started(max_agents=2)
                    try:
                        for _ in range(4):
                            r.tick()
                            r.wait(0.05)
                        self.assertEqual(r.jobs, [])
                    finally:
                        r.shutdown()
                self.assertEqual(self.board.get_task(tid).state, "verifying")
                self.assertEqual(self.board.get_task(done).state, "verified")
                self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), tip)
                pauses.remove(self.board, kind)

    def test_base_job_runs_under_token_free_kind_only(self):
        self.commit_file("base-news.txt", "news\n", "base moved on")
        pauses.add(self.board, "manual", "artel pause", now=1.0)
        r = self.started(max_agents=1)
        try:
            for _ in range(3):
                r.tick()
                self.assertEqual(r.jobs, [])     # no base job under a manual pause
            self.assertFalse(self.repo.is_ancestor("main", INTEGRATION))
            pauses.remove(self.board, "manual")
            pauses.add(self.board, "limit", "rejected", now=1.0, until=self.clock() + 3600)
            self.drive(r, lambda: self.repo.is_ancestor("main", INTEGRATION) and not r.jobs)
        finally:
            r.shutdown()


class BaseConflictTest(OpsCase):
    def test_base_conflict_pauses_and_notifies(self):
        self.commit_on(INTEGRATION, "shared.txt", "integration side\n")
        self.commit_file("shared.txt", "base side\n", "base side")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: "base_conflict" in pauses.active(self.board) and not r.jobs)
            (event,) = self.sent(r, "paused")
            self.assertEqual(event.text, "base_conflict: conflict: conflict in shared.txt")
        finally:
            r.shutdown()

    def test_base_job_error_notifies_one_line(self):
        self.commit_file("base-news.txt", "news\n", "base moved on")
        from artel import integrate
        with mock.patch.object(integrate, "base_update", side_effect=RuntimeError("base broke")):
            r = self.started(max_agents=1)
            try:
                self.drive(r, lambda: "base_conflict" in pauses.active(self.board))
                (event,) = self.sent(r, "paused")
            finally:
                r.shutdown()
        self.assertEqual(event.text, "base_conflict: base update failed, see the FAIL entry")
        (fail,) = self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'").fetchall()
        self.assertIn("base broke", fail["text"])

    def test_base_finish_error_names_the_log(self):
        self.commit_on(INTEGRATION, "integration.txt", "i\n")
        self.commit_file("base-news.txt", "news\n", "base moved on")   # merged and verified: it has a log
        from artel import integrate
        with mock.patch.object(integrate, "finish_base_update", side_effect=RuntimeError("finish broke")):
            r = self.started(max_agents=1)
            try:
                self.drive(r, lambda: "base_conflict" in pauses.active(self.board))
                (event,) = self.sent(r, "paused")
            finally:
                r.shutdown()
        self.assertRegex(event.text, r"^base_conflict: base update failed, see the FAIL entry; log \S*verify-1\.log$")
        self.assertNotIn("finish broke", event.text)


# --- the integration tip (PD-65 d) ----------------------------------------------------------------------------------

class IntegrationTipTest(OpsCase):
    def moved_text(self, new, old):
        return f"artel/integration at {new[:12]}, runner left it at {old[:12]}; resume adopts {new[:12]}"

    def test_foreign_move_of_integration_pauses(self):
        r = self.started(max_agents=1)
        try:
            old = git(self.repo_path, "rev-parse", INTEGRATION)
            self.assertEqual(self.board.get_meta("integration_tip"), old)
            new = self.commit_on(INTEGRATION, "foreign.txt", "x\n")
            r.tick()
            self.assertEqual(pauses.active(self.board)["integration_moved"].detail, self.moved_text(new, old))
            self.assertEqual([e.text for e in self.sent(r, "paused")],
                             ["integration_moved: " + self.moved_text(new, old)])
            r.tick()
            self.assertEqual(len(self.sent(r, "paused")), 1)
        finally:
            r.shutdown()

    def test_foreign_move_during_verify_job_is_not_adopted(self):
        gate = self.tmp / "gate"
        self.write_toml({"commands.verify": py_cmd(
            "import os, time\n"
            f"end = time.time() + 30\n"
            f"while not os.path.exists({str(gate)!r}) and time.time() < end: time.sleep(0.05)")})
        tid = self.verifying_task()
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).running_kind == "verify")
            old = git(self.repo_path, "rev-parse", INTEGRATION)
            new = self.commit_on(INTEGRATION, "foreign.txt", "x\n")
            gate.write_text("go", encoding="utf-8")
            self.drive(r, lambda: not r.jobs)
            self.assertEqual(self.board.get_task(tid).state, "verified")
            self.assertEqual(pauses.active(self.board)["integration_moved"].detail, self.moved_text(new, old))
            for _ in range(3):
                r.tick()
            self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), new)   # nothing fast-forwarded
            self.assertEqual(self.board.get_task(tid).state, "verified")
        finally:
            r.shutdown()

    def test_foreign_move_during_base_job_discards_its_result(self):
        gate = self.tmp / "gate"
        self.write_toml({"commands.verify": py_cmd(
            "import os, time\n"
            "end = time.time() + 30\n"
            f"while not os.path.exists({str(gate)!r}) and time.time() < end: time.sleep(0.05)")})
        self.commit_on(INTEGRATION, "integration.txt", "i\n")
        self.commit_file("base-news.txt", "news\n", "base moved on")   # a real merge, verified in a slot
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_meta("base_running"))
            old = git(self.repo_path, "rev-parse", INTEGRATION)
            new = self.commit_on(INTEGRATION, "foreign.txt", "x\n")
            gate.write_text("go", encoding="utf-8")
            self.drive(r, lambda: not r.jobs)
            self.assertEqual(pauses.active(self.board)["integration_moved"].detail, self.moved_text(new, old))
            self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), new)   # the merge was not applied
        finally:
            r.shutdown()

    def test_move_just_before_integrate_is_caught(self):
        # the tick's own check passed; the ref moves before integrate_task: it must be checked again right there
        tid = self.add_task("v")
        self.task_branch_with_commit(tid)
        self.set_task(tid, state="verified", round=1)
        self.board.set_meta(f"verified:{tid}", git(self.repo_path, "rev-parse", task_branch(tid)))
        r = self.started(max_agents=1)
        moved = []
        real = r.update_pauses

        def update_then_move():
            real()
            if not moved:
                moved.append(self.commit_on(INTEGRATION, "foreign.txt", "x\n"))

        r.update_pauses = update_then_move
        try:
            r.tick()
            self.assertEqual(self.board.get_task(tid).state, "verified")
            self.assertIn("integration_moved", pauses.active(self.board))
            self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), moved[0])
        finally:
            r.shutdown()

    def test_integrate_error_after_the_move_keeps_the_tip(self):
        # integrate_task moved the ref and then failed: the runner's own move is not a foreign one
        tid = self.add_task("v")
        commit = self.task_branch_with_commit(tid)
        self.set_task(tid, state="verified", round=1)
        self.board.set_meta(f"verified:{tid}", commit)
        r = self.started(max_agents=1)
        real = transitions.apply

        def broken(board, task_id, event, *a, **kw):
            if event == "ff_ok":
                raise RuntimeError("transition broke")
            return real(board, task_id, event, *a, **kw)

        try:
            with mock.patch.object(transitions, "apply", broken):
                r.tick()
            self.assertEqual(git(self.repo_path, "rev-parse", INTEGRATION), commit)
            r.tick()
            self.assertNotIn("integration_moved", pauses.active(self.board))
        finally:
            r.shutdown()

    def test_resume_adopts_moved_integration(self):
        r = self.started(max_agents=1)
        try:
            new = self.commit_on(INTEGRATION, "foreign.txt", "x\n")
            r.tick()
            self.assertIn("integration_moved", pauses.active(self.board))
            requests.enqueue(self.repo, self.board, "resume", None, {"force": False})
            r.tick()
            self.assertEqual(pauses.active(self.board), {})
            self.assertEqual(self.board.get_meta("integration_tip"), new)
            r.tick()
            self.assertEqual(pauses.active(self.board), {})
        finally:
            r.shutdown()

    def test_runner_integration_does_not_trip_the_check(self):
        self.commit_file("base-news.txt", "news\n", "base moved on")   # a fast-forward base update first
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).state == "done", timeout=40)
            r.tick()
            self.assertEqual(pauses.active(self.board), {})
            self.assertEqual(self.board.get_meta("integration_tip"), git(self.repo_path, "rev-parse", INTEGRATION))
            self.assertTrue(self.repo.is_ancestor("main", INTEGRATION))
        finally:
            r.shutdown()

    def test_move_during_base_conflict_is_allowed(self):
        pauses.add(self.board, "base_conflict", "conflict: conflict in a.txt", now=1.0)
        r = self.started(max_agents=1)
        try:
            new = self.commit_on(INTEGRATION, "repair.txt", "fixed\n")   # the human repairs the branch
            r.tick()
            self.assertEqual(set(pauses.active(self.board)), {"base_conflict"})
            requests.enqueue(self.repo, self.board, "resume", None, {"force": False})
            r.tick()
            r.tick()
            self.assertEqual(pauses.active(self.board), {})
            self.assertEqual(self.board.get_meta("integration_tip"), new)
        finally:
            r.shutdown()


# --- stop and signals (§5, §12, PD-26) ------------------------------------------------------------------------------

class StopTest(OpsCase):
    def test_stop_file_soft_waits_for_running_stage(self):
        self.scenario({"write": [{"sleep": 1.0}] + WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).running)
            self.repo.stop_path.write_text("soft", encoding="utf-8")
            r.tick()
            self.assertTrue(r.stopping)
            self.assertEqual(len(r.jobs), 1)                # not terminated
            self.drive(r, lambda: not r.jobs)
        finally:
            r.shutdown()
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("reviewing", 0, False))
        self.assertEqual(len(self.calls()), 1)

    def test_stop_now_terminates_and_requeues(self):
        self.scenario({"write": SLEEP})
        tid = self.add_task("t")
        r = self.started(max_agents=1)
        try:
            self.drive(r, lambda: self.board.get_task(tid).running_pid)
            pid = self.board.get_task(tid).running_pid
            self.repo.stop_path.write_text("now", encoding="utf-8")
            self.drive(r, lambda: not r.jobs, timeout=20)
            self.assertFalse(platform.pid_alive(pid))
        finally:
            r.shutdown()
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.running, task.attempts), ("writing", False, 0))

    def test_stage_timeout_counts_attempt(self):
        self.scenario({"write": SLEEP})
        tid = self.add_task("t")
        r = self.started(max_agents=1, stage_timeout="1s", kill_grace="1s")
        try:
            self.drive(r, lambda: self.board.get_task(tid).attempts >= 1, timeout=30)
        finally:
            r.shutdown()
        (first, *_) = [e.text for e in journal.task_entries(self.board, tid) if e.kind == "VERDICT"]
        self.assertIn("timed out", first)

    def runner_process(self, out_path):
        self.write_toml({"notify.command": py_cmd(
            "import sys\n"
            f"with open({str(out_path)!r}, 'a', encoding='utf-8') as fh: fh.write(sys.stdin.read() + '\\n')"),
            "notify.telegram_enabled": False})
        return subprocess.Popen([sys.executable, "-m", "artel", "run", "-n", "1"], cwd=self.repo_path,
                                env=subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **platform.popen_kwargs())

    def soft_stop_case(self, signals: int):
        self.scenario({"write": [{"sleep": 2.0}] + WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        out_path = self.tmp / "notified.jsonl"
        proc = self.runner_process(out_path)
        try:
            self.wait_until(lambda: self.board.get_task(tid).running, timeout=20)
            for _ in range(signals):
                platform.soft_terminate(proc.pid, platform.group_of(proc))
                time.sleep(0.2)
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stderr, b"")   # neither the handler nor the stop prints anything
        task = self.board.get_task(tid)
        self.assertEqual((task.state, task.attempts, task.running), ("reviewing", 0, False))   # finished, not killed
        self.assertEqual(len(self.calls()), 1)
        return out_path.read_text(encoding="utf-8") if out_path.exists() else ""

    def test_signal_soft_stop_and_runner_stopped_notification(self):
        sent = self.soft_stop_case(signals=1)
        self.assertIn('"kind": "runner_stopped"', sent)

    def test_second_signal_still_soft(self):
        self.soft_stop_case(signals=2)


# --- notifications ----------------------------------------------------------------------------------------------

class NotifierTest(OpsCase):
    def test_slow_notifier_does_not_block_tick(self):
        out_path = self.tmp / "slow.txt"
        command = py_cmd("import sys, time\ntime.sleep(5)\n"
                         f"open({str(out_path)!r}, 'w', encoding='utf-8').write(sys.stdin.read())")
        r = self.started()
        r.notify = notify.Dispatcher(["paused"], command=notify.CommandNotifier(command, self.cfg["shell"])).emit
        try:
            requests.enqueue(self.repo, self.board, "pause", None, {})
            started = time.monotonic()
            r.tick()
            self.assertLess(time.monotonic() - started, 1.0)
            self.wait_until(lambda: out_path.exists() and "paused" in out_path.read_text(encoding="utf-8"),
                            timeout=30)
        finally:
            r.shutdown()

    def test_shutdown_drops_undelivered_events_after_kill_grace(self):
        release = threading.Event()
        r = self.started(kill_grace="1s")
        r.notify = lambda event: release.wait(30)
        err = io.StringIO()
        try:
            r.pause("manual", "one")
            r.pause("disk", "two")
            started = time.monotonic()
            with mock.patch("sys.stderr", err), mock.patch.object(runner_module, "NOTIFY_FLUSH_MIN", 0.0):
                r.shutdown()
            self.assertLess(time.monotonic() - started, 5)
        finally:
            release.set()
        lines = [x for x in err.getvalue().splitlines() if "not sent" in x]
        self.assertEqual(len(lines), 1, err.getvalue())

    def test_a_raising_notifier_is_journaled_once(self):
        r = self.started()
        r.notify = mock.Mock(side_effect=RuntimeError("notifier down"))
        try:
            r.pause("manual", "one")
            self.assertTrue(r.flush_notifications(10))
            r.tick()
            r.tick()           # a clean tick: the same error later is journaled again
            r.pause("disk", "two")
            self.assertTrue(r.flush_notifications(10))
            r.tick()
        finally:
            r.shutdown()
        rows = self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'").fetchall()
        self.assertEqual(len([x for x in rows if "notifier down" in x["text"]]), 2)


# --- log retention (§5) -------------------------------------------------------------------------------------------

class LogRetentionTest(OpsCase):
    def test_log_retention_keeps_last_n(self):
        ids = []
        for n in range(4):
            tid = self.add_task(f"t{n}")
            (self.repo.logs_dir / str(tid)).mkdir(parents=True)
            (self.repo.logs_dir / str(tid) / "write-1.jsonl").write_text("{}\n", encoding="utf-8")
            ids.append(tid)
        for tid in (ids[1], ids[2], ids[0]):   # finished in this order: "newest" is by finishing, not by id
            self.set_task(tid, state="done")
            time.sleep(0.02)   # distinct updated_at
        self.set_task(ids[3], state="writing")
        r = self.started(log_keep_tasks=2)
        try:
            kept = lambda: [t for t in ids if (self.repo.logs_dir / str(t)).is_dir()]   # noqa: E731
            self.assertEqual(kept(), [ids[0], ids[2], ids[3]])   # the first finished lost its logs; in flight kept
            self.set_task(ids[3], state="rejected")
            r.tick()
            self.assertEqual(kept(), [ids[0], ids[3]])
        finally:
            r.shutdown()


# --- T15 review leftovers -----------------------------------------------------------------------------------------

class T15LeftoversTest(OpsCase):
    def test_tick_survives_board_failure_in_guard(self):
        err = io.StringIO()
        broken = sqlite3.OperationalError("database is locked")
        r = self.started()
        try:
            with mock.patch.object(r, "_collect", side_effect=RuntimeError("collect broke")), \
                    mock.patch.object(journal, "append", side_effect=broken), mock.patch("sys.stderr", err):
                r.tick()
                r.tick()
        finally:
            r.shutdown()
        self.assertIn("internal error in collecting results", err.getvalue())
        # run_forever keeps going and ends normally
        r = self.runner()
        result = []
        with mock.patch.object(runner_module.Runner, "_collect", side_effect=RuntimeError("collect broke")), \
                mock.patch.object(journal, "append", side_effect=broken), mock.patch("sys.stderr", io.StringIO()):
            thread = threading.Thread(target=lambda: result.append(r.run_forever()))
            thread.start()
            time.sleep(0.5)
            r.request_stop(now=False)
            thread.join(20)
        self.assertEqual(result, [0])

    def test_last_tick_error_resets_after_a_clean_tick(self):
        r = self.started()
        try:
            for broken in (True, False, True):
                if broken:
                    with mock.patch.object(r, "_collect", side_effect=RuntimeError("collect broke")):
                        r.tick()
                else:
                    r.tick()
        finally:
            r.shutdown()
        rows = self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'").fetchall()
        self.assertEqual(len([x for x in rows if "collect broke" in x["text"]]), 2)

    def test_shutdown_uses_one_shared_deadline(self):
        r = self.started(kill_grace="1s")
        release = threading.Event()
        for n in range(3):
            stuck = threading.Thread(target=release.wait, daemon=True)
            stuck.start()
            r.jobs.append(Job("stage", 100 + n, n + 1, stuck, threading.Event()))
        err = io.StringIO()
        try:
            with mock.patch.object(procs, "JOIN_DEADLINE", 1.0), mock.patch("sys.stderr", err):
                started = time.monotonic()
                r.shutdown()
            self.assertLess(time.monotonic() - started, 4)   # one 2 s deadline, not 2 s per job
        finally:
            release.set()
        self.assertEqual(err.getvalue().count("did not stop"), 3)

    def test_base_job_shutdown_warning_names_no_task(self):
        r = self.started(kill_grace="0s")
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, daemon=True)
        stuck.start()
        r.jobs.append(Job("base", None, 1, stuck, threading.Event()))
        err = io.StringIO()
        try:
            with mock.patch.object(procs, "JOIN_DEADLINE", 0.2), mock.patch("sys.stderr", err):
                r.shutdown()
        finally:
            release.set()
        self.assertIn("warning: base job did not stop", err.getvalue())
        self.assertNotIn("#None", err.getvalue())

    def test_job_end_not_delayed_by_poll_interval(self):
        r = self.started(poll_interval="30s")
        go = threading.Event()
        job = Job("stage", None, 1, None, threading.Event(), discarded=True)   # type: ignore[arg-type]

        def body():
            go.wait(10)
            job.finished = True
            r._wake.set()      # as a job's finally does, a moment before its thread ends
            time.sleep(0.3)

        job.thread = threading.Thread(target=body, daemon=True)
        job.thread.start()
        r.jobs.append(job)
        try:
            r.tick()
            self.assertIn(job, r.jobs)
            go.set()
            started = time.monotonic()
            while job in r.jobs and time.monotonic() - started < 40:
                r.wait(30)
                r.tick()
            self.assertLess(time.monotonic() - started, 5)
        finally:
            r.shutdown()

    def test_finish_failed_event_carries_state(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        r = self.started(max_agents=1)
        try:
            with mock.patch.object(stages, "finish_stage", side_effect=RuntimeError("bookkeeping broke")):
                self.drive(r, lambda: self.board.get_task(tid).attempts >= 1 and not r.jobs)
            event = self.sent(r, "error")[0]
        finally:
            r.shutdown()
        self.assertEqual(event.state, "writing")
        self.assertEqual(event.task_id, tid)

    def test_held_task_not_released_by_a_token_free_pause(self):
        tid = self.add_task("held")
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        human = self.tmp / "human"
        git(self.repo_path, "worktree", "add", "-q", str(human), task_branch(tid))
        r = self.started(max_agents=1)
        try:
            r.tick()
            pauses.add(self.board, "quota", "five_hour at 96%", now=1.0, until=self.clock() + 3600)
            r.tick()
            pauses.remove(self.board, "quota")
            r.tick()
            lines = [e.text for e in journal.task_entries(self.board, tid) if e.author == "runner"]
            self.assertEqual(lines, [f"held: artel/{tid} checked out at {human}"])
        finally:
            r.shutdown()

    def test_an_unreadable_pause_set_does_not_end_the_tick(self):
        r = self.started()
        try:
            with mock.patch.object(pauses, "blocks_verify", side_effect=ValueError("bad json")):
                r.tick()
            rows = self.board.conn.execute("SELECT text FROM entries WHERE kind = 'FAIL'").fetchall()
            self.assertTrue(any("reading the pauses" in x["text"] for x in rows))
        finally:
            r.shutdown()

    def test_stop_now_ends_run_forever_despite_a_stuck_job(self):
        r = self.runner(kill_grace="0s")
        result = []
        release = threading.Event()
        with mock.patch.object(procs, "JOIN_DEADLINE", 0.5), mock.patch("sys.stderr", io.StringIO()):
            thread = threading.Thread(target=lambda: result.append(r.run_forever()))
            thread.start()
            try:
                self.wait_until(lambda: r._started, timeout=10)
                stuck = threading.Thread(target=release.wait, daemon=True)
                stuck.start()
                r.jobs.append(Job("stage", None, 1, stuck, threading.Event(), discarded=True))
                r.request_stop(now=True)
                thread.join(15)
                self.assertFalse(thread.is_alive())
            finally:
                release.set()
                thread.join(10)
        self.assertEqual(result, [0])

    def test_deleted_human_worktree_does_not_hold(self):
        self.scenario({"write": WRITE, "review": PASS})
        tid = self.add_task("Add hello")
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        human = self.tmp / "human"
        git(self.repo_path, "worktree", "add", "-q", str(human), task_branch(tid))
        r = self.started(max_agents=1)    # start prunes once (reconcile); later ticks must prune themselves
        try:
            r.tick()
            self.assertEqual(self.board.get_task(tid).state, "open")   # held
            shutil.rmtree(human)              # deleted without `git worktree remove`
            self.drive(r, lambda: self.board.get_task(tid).state != "open")
        finally:
            r.shutdown()

    def test_held_task_journaled_once(self):
        tid = self.add_task("held")
        git(self.repo_path, "branch", task_branch(tid), INTEGRATION)
        human = self.tmp / "human"
        git(self.repo_path, "worktree", "add", "-q", str(human), task_branch(tid))
        r = self.started(max_agents=1)

        def lines():
            return [e.text for e in journal.task_entries(self.board, tid) if e.author == "runner"]
        try:
            for _ in range(3):
                r.tick()
            self.assertEqual(lines(), [f"held: artel/{tid} checked out at {human}"])
            self.assertEqual(r.jobs, [])
            git(self.repo_path, "worktree", "remove", "--force", str(human))
            self.scenario({"write": SLEEP})
            r.tick()
            self.assertEqual(lines(), [f"held: artel/{tid} checked out at {human}", f"released: artel/{tid}"])
        finally:
            r.shutdown()

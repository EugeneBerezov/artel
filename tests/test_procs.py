import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from artel import platform as P, procs
from artel.errors import ArtelError
from tests.helpers import IGNORE_SOFT_STOP, py_cmd, subprocess_env

TREE = ("import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n")

# the leader starts a grandchild that inherits stdout and exits at once, leaving the pipe held open
ORPHAN = ("import subprocess, sys\n"
          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
          "print(c.pid, flush=True)\n")


def wait_dead(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while P.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not P.pid_alive(pid)


class RunTreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_py(self, code, **kw):
        kw.setdefault("timeout", 30.0)
        kw.setdefault("kill_grace", 1.0)
        kw.setdefault("env", subprocess_env())   # UTF-8 stdio in the child: Windows defaults to cp1252
        return procs.run_tree(py_cmd(code), cwd=self.dir, **kw)

    def test_returncode_and_lines(self):
        lines = []
        log = self.dir / "logs" / "w-1.jsonl"
        res = self.run_py("import sys\nprint('привет ✓')\nprint('two')\nsys.exit(7)\n", log_path=log,
                          on_line=lines.append)
        self.assertEqual((res.returncode, res.timed_out, res.stopped), (7, False, False))
        self.assertEqual(lines, ["привет ✓", "two"])
        self.assertEqual(log.read_bytes(), "привет ✓\ntwo\n".encode("utf-8"))   # UTF-8, "\n" only
        self.assertGreaterEqual(res.duration, 0)

    def test_stdin_is_delivered(self):
        lines = []
        big = "строка ✓\n" * 20000   # larger than a pipe buffer: delivery must not deadlock
        self.run_py("import sys\ndata = sys.stdin.read()\nprint(len(data))\n", stdin_text=big, on_line=lines.append)
        self.assertEqual(lines, [str(len(big))])

    def test_stdin_attached_even_when_empty(self):
        lines = []
        res = self.run_py("import sys\nprint(repr(sys.stdin.read()))\n", on_line=lines.append, timeout=10.0)
        self.assertEqual(lines, ["''"])
        self.assertEqual(res.returncode, 0)

    def test_stderr_to_side_file_or_merged(self):
        code = "import sys\nprint('out', flush=True)\nprint('err', file=sys.stderr, flush=True)\n"
        log = self.dir / "a.log"
        lines = []
        self.run_py(code, log_path=log, on_line=lines.append)
        self.assertEqual(lines, ["out"])
        self.assertEqual(log.read_text(encoding="utf-8"), "out\n")
        self.assertEqual((self.dir / "a.log.stderr").read_text(encoding="utf-8"), "err\n")
        merged_log, merged = self.dir / "b.log", []
        self.run_py(code, log_path=merged_log, merge_stderr=True, on_line=merged.append)
        self.assertCountEqual(merged, ["out", "err"])
        self.assertFalse((self.dir / "b.log.stderr").exists())

    def test_stderr_is_drained_without_a_log(self):
        # catches: an undrained stderr pipe blocking a chatty child forever
        code = "import sys\nsys.stderr.write('x' * 500000)\nprint('done')\n"
        lines = []
        res = self.run_py(code, on_line=lines.append, timeout=20.0)
        self.assertEqual((res.returncode, lines, res.timed_out), (0, ["done"], False))

    def test_on_start_reports_pid_group_fingerprint(self):
        seen = []
        lines = []
        self.run_py("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(0.5)\n", on_start=lambda *a: seen.append(a), on_line=lines.append)
        self.assertEqual(len(seen), 1)
        pid, group, fingerprint = seen[0]
        self.assertEqual(str(pid), lines[0])
        self.assertEqual(group, pid)   # own process group (POSIX) / group id is the pid (Windows)
        self.assertTrue(fingerprint)   # catches dropping the fingerprint from on_start

    def test_timeout_kills_grandchildren(self):
        lines = []
        res = procs.run_tree(py_cmd(TREE), cwd=self.dir, timeout=3.0, kill_grace=1.0, on_line=lines.append)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.stopped)
        grandchild = int(lines[0])
        self.assertTrue(wait_dead(grandchild))

    def test_deadline_callable_is_reevaluated(self):
        """Mutation caught: the deadline read once (a lock wait that extends it while the agent runs is lost)
        or `timeout` still applied when a deadline is given (PD-74)."""
        sleeper = py_cmd("import time; time.sleep(60)")
        calls = []
        start = time.monotonic()

        def deadline():
            calls.append(1)
            if time.monotonic() - start < 1.0:
                return time.monotonic() + 5.0   # extended again at every poll while the first second runs
            return time.monotonic() - 1         # then due
        res = procs.run_tree(sleeper, cwd=self.dir, timeout=0.1, deadline=deadline, kill_grace=1.0)
        self.assertTrue(res.timed_out)
        self.assertGreater(len(calls), 3)                    # read at each poll
        self.assertGreater(time.monotonic() - start, 0.9)    # `timeout=0.1` did not end it, nor the first value
        res = procs.run_tree(py_cmd("print('ok')"), cwd=self.dir, timeout=0.1, kill_grace=1.0,
                             deadline=lambda: time.monotonic() + 30)
        self.assertEqual((res.returncode, res.timed_out), (0, False))

    def test_soft_stop_ignored_gets_hard_kill(self):
        code = IGNORE_SOFT_STOP + "import time\nprint('up', flush=True)\ntime.sleep(60)\n"
        up = threading.Event()
        start = time.monotonic()

        def on_line(line):
            up.set()

        # the timeout starts at spawn; 3 s leaves room for interpreter start so the handler is installed
        res = procs.run_tree(py_cmd(code), cwd=self.dir, timeout=3.0, kill_grace=0.5, on_line=on_line)
        self.assertTrue(up.is_set())
        self.assertTrue(res.timed_out)
        self.assertLess(time.monotonic() - start, 12)

    def test_stop_event_terminates(self):
        lines = []
        stop = threading.Event()

        def on_line(line):   # stop once the child has printed its grandchild's pid
            lines.append(line)
            stop.set()

        res = procs.run_tree(py_cmd(TREE), cwd=self.dir, timeout=60.0, kill_grace=1.0, on_line=on_line,
                             stop_event=stop)
        self.assertTrue(res.stopped)
        self.assertFalse(res.timed_out)
        self.assertLess(res.duration, 20)
        self.assertTrue(wait_dead(int(lines[0])))

    def test_stop_event_already_set(self):
        stop = threading.Event()
        stop.set()
        res = procs.run_tree(py_cmd("import time\ntime.sleep(60)\n"), cwd=self.dir, timeout=60.0, kill_grace=1.0,
                             stop_event=stop)
        self.assertTrue(res.stopped)
        self.assertLess(res.duration, 20)

    def test_leader_exit_sweeps_leftover_children_and_returns(self):
        # catches: waiting forever on a pipe held open by an orphan
        lines = []
        start = time.monotonic()
        res = procs.run_tree(py_cmd(ORPHAN), cwd=self.dir, timeout=60.0, kill_grace=1.0, on_line=lines.append)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        self.assertLess(time.monotonic() - start, 15)
        self.assertTrue(wait_dead(int(lines[0])))

    @unittest.skipIf(P.IS_WINDOWS, "needs a process outside the job/group")
    def test_escaped_pipe_holder_is_abandoned_quietly(self):
        """Mutation caught: a pump that keeps reporting after run_tree returned; an unbounded join."""
        code = ("import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3.0); print(\"late\", "
                "flush=True)'], start_new_session=True)\n"
                "print('first', flush=True)\n")
        lines = []
        saved = procs.JOIN_DEADLINE
        procs.JOIN_DEADLINE = 0.5
        self.addCleanup(setattr, procs, "JOIN_DEADLINE", saved)
        start = time.monotonic()
        res = self.run_py(code, on_line=lines.append)
        self.assertLess(time.monotonic() - start, 2.5)   # returned at the deadline, not when the pipe closed
        self.assertEqual(res.returncode, 0)
        time.sleep(3.5)
        self.assertEqual(lines, ["first"])               # "late" arrived after run_tree returned: dropped

    def test_trailing_cr_is_not_stripped_twice(self):
        """Mutation caught: rstrip("\\r\\n") eating every trailing CR."""
        lines = []
        self.run_py("import sys\nsys.stdout.buffer.write(b'a\\r\\r\\nb\\n')\n", on_line=lines.append)
        self.assertEqual(lines, ["a\r", "b"])

    def test_empty_argv_is_artel_error(self):
        with self.assertRaises(ArtelError):
            procs.run_tree([], cwd=self.dir, timeout=1.0, kill_grace=1.0)

    def test_unstartable_command_is_artel_error(self):
        with self.assertRaises(ArtelError):
            procs.run_tree([str(self.dir / "no-such-binary")], cwd=self.dir, timeout=5.0, kill_grace=1.0)

    def test_on_line_error_is_raised_after_the_run(self):
        # catches: stopping the reader after a callback error (the child would block on a full pipe)
        def boom(line):
            raise RuntimeError("callback failed")

        code = "for i in range(3000):\n    print('x' * 100, flush=True)\n"
        start = time.monotonic()
        with self.assertRaises(RuntimeError):
            self.run_py(code, on_line=boom, timeout=20.0)
        self.assertLess(time.monotonic() - start, 15)

    def test_unwritable_log_does_not_block_the_child(self):
        # catches: a log write error ending the reader (the child blocks on a full pipe until the timeout)
        blocker = self.dir / "file"
        blocker.write_text("x", encoding="utf-8")
        code = "for i in range(3000):\n    print('x' * 100, flush=True)\n"
        start = time.monotonic()
        with self.assertRaises(OSError):
            self.run_py(code, log_path=blocker / "log", timeout=20.0)
        self.assertLess(time.monotonic() - start, 15)

    def test_log_is_appended_not_truncated(self):
        log = self.dir / "run.log"
        log.write_text("earlier\n", encoding="utf-8")
        self.run_py("print('now')", log_path=log)
        self.assertEqual(log.read_text(encoding="utf-8"), "earlier\nnow\n")

    def test_child_may_write_a_lot_before_reading_stdin(self):
        # catches: feeding stdin synchronously (deadlocks when the child fills its stdout first)
        code = ("import sys\nfor i in range(3000):\n    print('x' * 100, flush=True)\n"
                "print(len(sys.stdin.read()))\n")
        lines = []
        res = self.run_py(code, stdin_text="y" * 300000, on_line=lines.append, timeout=30.0)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(lines[-1], "300000")

    def test_newlines_are_not_translated(self):
        lines = []
        self.run_py("import sys\nd = sys.stdin.buffer.read()\nprint(d.count(b'\\r'), len(d), flush=True)\n",
                    stdin_text="a\nb\n", on_line=lines.append)
        self.assertEqual(lines, ["0 4"])

    def test_on_start_error_kills_the_tree(self):
        pids = []

        def bad_start(pid, group, fingerprint):
            pids.append(pid)
            raise RuntimeError("recording failed")

        with self.assertRaises(RuntimeError):
            procs.run_tree(py_cmd("import time\ntime.sleep(60)\n"), cwd=self.dir, timeout=60.0, kill_grace=1.0,
                           on_start=bad_start)
        self.assertTrue(wait_dead(pids[0]))

    def test_env_and_cwd_are_applied(self):
        lines = []
        env = {"ARTEL_PROBE": "42"}
        env.update({k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH")})
        self.run_py("import os\nprint(os.environ.get('ARTEL_PROBE'))\nprint(os.getcwd())\n", env=env,
                    on_line=lines.append)
        self.assertEqual(lines[0], "42")
        self.assertEqual(Path(lines[1]).resolve(), self.dir.resolve())

    def test_cmd_string_goes_through_command_line(self):
        seen = []
        real = P.command_line

        def record(argv, **kw):
            seen.append(list(argv))
            return real(argv, **kw)

        argv = py_cmd("print('x')")
        with mock.patch.object(P, "command_line", record):
            procs.run_tree(argv, cwd=self.dir, timeout=10.0, kill_grace=1.0)
        self.assertEqual(seen, [argv])


class CommandArgvTest(unittest.TestCase):
    def test_command_argv_string_uses_shell_list_runs_direct(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            res = procs.run_tree(procs.command_argv("exit 3", "auto"), cwd=Path(tmp), timeout=20.0, kill_grace=1.0)
            self.assertEqual(res.returncode, 3)
            res = procs.run_tree(procs.command_argv(py_cmd("import sys; sys.exit(4)"), "auto"), cwd=Path(tmp),
                                 timeout=20.0, kill_grace=1.0)
            self.assertEqual(res.returncode, 4)

    def test_command_argv_list_resolves_executable(self):
        argv = procs.command_argv([sys.executable, "-c", "pass"], "auto")
        self.assertEqual(argv[1:], ["-c", "pass"])
        with self.assertRaises(ArtelError):
            procs.command_argv(["definitely-not-a-real-binary-xyz"], "auto")

    def test_heavy_argv_forms(self):
        command = py_cmd("print(1)")
        marker = str(Path(__file__).resolve())
        self.assertEqual(procs.heavy_argv(command, "", "sh"), procs.command_argv(command, "sh"))
        self.assertEqual(procs.heavy_argv(command, [], "sh"), procs.command_argv(command, "sh"))
        listed = procs.heavy_argv(command, [sys.executable, marker], "sh")
        self.assertEqual(listed, [P.which(sys.executable) or sys.executable, marker] + procs.command_argv(command, "sh"))
        prefix = "flock /tmp/x"
        stringed = procs.heavy_argv(command, prefix, "sh")
        self.assertEqual(stringed, P.prefix_argv("sh", prefix, procs.command_argv(command, "sh")))
        self.assertEqual(stringed[1:4], ["-c", prefix + ' "$@"', "artel-heavy"])
        self.assertEqual(stringed[4:], procs.command_argv(command, "sh"))
        # a string command goes through the shell inside the prefix too
        self.assertEqual(procs.heavy_argv("make all", prefix, "sh")[4:], procs.command_argv("make all", "sh"))

    def test_empty_command_rejected(self):
        for bad in ("", "   ", []):
            with self.assertRaises(ArtelError, msg=repr(bad)):
                procs.command_argv(bad, "auto")
            with self.assertRaises(ArtelError, msg=repr(bad)):
                procs.heavy_argv(bad, "", "auto")
        with self.assertRaises(ArtelError):
            procs.command_argv([5], "auto")   # type: ignore[list-item]

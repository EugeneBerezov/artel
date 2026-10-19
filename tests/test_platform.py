import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from artel import platform as P
from artel.errors import ArtelError
from tests.helpers import IGNORE_SOFT_STOP, REPO_ROOT, py_cmd, subprocess_env

TREE = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
time.sleep(60)
"""

ORPHAN = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
"""


def wait_dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while P.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not P.pid_alive(pid)


class LockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "runner.lock"

    def probe(self):
        code = ("import sys\nfrom pathlib import Path\nfrom artel.platform import FileLock\n"
                "lock = FileLock(Path(sys.argv[1]))\nprint(lock.acquire(), flush=True)\nlock.release()\n")
        out = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True,
                             encoding="utf-8", env=subprocess_env(), cwd=REPO_ROOT)
        return out.stdout.strip()

    def test_lock_excludes_other_process(self):
        lock = P.FileLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertEqual(self.probe(), "False")
        lock.release()
        self.assertEqual(self.probe(), "True")

    def test_release_idempotent(self):
        lock = P.FileLock(self.path)
        lock.acquire()
        lock.release()
        lock.release()

    def test_lock_creates_parent_dir(self):
        lock = P.FileLock(self.path)
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquire())
        self.assertTrue(self.path.parent.is_dir())


class TreeTest(unittest.TestCase):
    def spawn(self, code):
        proc = subprocess.Popen(py_cmd(code), stdout=subprocess.PIPE, encoding="utf-8", **P.popen_kwargs())
        P.register_tree(proc)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: P.hard_kill(proc.pid, P.group_of(proc)))
        self.addCleanup(proc.stdout.close)
        return proc

    def test_terminate_tree_kills_grandchild(self):
        proc = self.spawn(TREE)
        grandchild = int(proc.stdout.readline())
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=2.0))
        proc.wait(timeout=10)
        self.assertTrue(wait_dead(grandchild))

    def test_soft_stop_ignored_gets_hard_kill(self):
        proc = self.spawn(IGNORE_SOFT_STOP + "import time\nprint('up', flush=True)\ntime.sleep(60)\n")
        proc.stdout.readline()
        start = time.monotonic()
        P.terminate_tree(proc.pid, P.group_of(proc), grace=0.5)
        proc.wait(timeout=10)
        self.assertLess(time.monotonic() - start, 8)

    def test_tree_alive_false_after_exit(self):
        proc = self.spawn("print('x')")
        proc.wait(timeout=10)
        self.assertTrue(wait_dead(proc.pid))
        self.assertFalse(P.tree_alive(proc.pid, P.group_of(proc)))

    def test_terminate_skips_on_fingerprint_mismatch(self):
        proc = self.spawn(TREE)
        proc.stdout.readline()
        self.assertFalse(P.terminate_tree(proc.pid, P.group_of(proc), grace=0.5, fingerprint="bogus"))
        self.assertIsNone(proc.poll())

    def test_job_object_kills_orphaned_grandchild(self):
        proc = self.spawn(ORPHAN)
        grandchild = int(proc.stdout.readline())
        proc.wait(timeout=10)
        P.hard_kill(proc.pid, P.group_of(proc))
        self.assertTrue(wait_dead(grandchild))


class PidTest(unittest.TestCase):
    def test_pid_alive_self_true(self):
        self.assertTrue(P.pid_alive(os.getpid()))

    def test_pid_alive_exited_false(self):
        proc = subprocess.Popen(py_cmd("pass"))
        proc.wait(timeout=10)
        self.assertFalse(P.pid_alive(proc.pid))

    def test_fingerprint_stable_for_self(self):
        first = P.process_fingerprint(os.getpid())
        self.assertTrue(first)
        self.assertEqual(first, P.process_fingerprint(os.getpid()))

    def test_fingerprint_differs_for_another_process(self):
        proc = subprocess.Popen(py_cmd("import time; time.sleep(30)"))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        other = P.process_fingerprint(proc.pid)
        self.assertTrue(other)
        self.assertNotEqual(other, P.process_fingerprint(os.getpid()))

    def test_fingerprint_none_for_exited_pid(self):
        proc = subprocess.Popen(py_cmd("pass"))
        proc.wait(timeout=10)
        self.assertIsNone(P.process_fingerprint(proc.pid))


class StopHandlerTest(unittest.TestCase):
    def test_soft_terminate_runs_callback(self):
        code = ("import sys, time\nfrom artel.platform import install_stop_handlers\n"
                "def cb():\n    print('stopped', flush=True)\n    sys.exit(0)\n"
                "install_stop_handlers(cb)\nprint('up', flush=True)\n[time.sleep(0.1) for _ in range(600)]\n")
        proc = subprocess.Popen(py_cmd(code), stdout=subprocess.PIPE, encoding="utf-8",
                                env=subprocess_env(), cwd=REPO_ROOT, **P.popen_kwargs())
        P.register_tree(proc)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: P.hard_kill(proc.pid, P.group_of(proc)))
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        P.soft_terminate(proc.pid, P.group_of(proc))
        out = proc.stdout.read()
        proc.wait(timeout=10)
        self.assertIn("stopped", out)


class ShellTest(unittest.TestCase):
    def test_shell_argv_variants(self):
        self.assertEqual(P.shell_argv("auto", "x", windows=False), ["sh", "-c", "x"])
        orig = P.find_git_bash
        P.find_git_bash = lambda: "C:/Git/bin/bash.exe"
        try:
            self.assertEqual(P.shell_argv("auto", "x", windows=True), ["C:/Git/bin/bash.exe", "-c", "x"])
        finally:
            P.find_git_bash = orig
        self.assertEqual(P.shell_argv("pwsh", "x", windows=False),
                         ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "x"])
        self.assertEqual(P.shell_argv("cmd", "x", windows=True), ["cmd", "/d", "/s", "/c", "x"])
        with self.assertRaises(ArtelError):
            P.shell_argv("fish", "x", windows=False)

    def test_prefix_argv_sh(self):
        self.assertEqual(
            P.prefix_argv("auto", "tools/locks.sh heavy artel --wait --", ["npm", "test"], windows=False),
            ["sh", "-c", 'tools/locks.sh heavy artel --wait -- "$@"', "artel-heavy", "npm", "test"])

    def test_prefix_argv_refused_for_pwsh(self):
        for shell in ("pwsh", "cmd"):
            with self.assertRaises(ArtelError) as ctx:
                P.prefix_argv(shell, "x", ["a"], windows=False)
            self.assertIn("must be a list", str(ctx.exception))

    def test_git_bash_candidates_order(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "Git"
            (root / "usr" / "bin").mkdir(parents=True)
            (root / "usr" / "bin" / "bash.exe").write_bytes(b"")
            (root / "bin").mkdir()
            (root / "bin" / "bash.exe").write_bytes(b"")
            exec_path = str(root / "mingw64" / "libexec" / "git-core")
            got = P.git_bash_candidates(exec_path, {"CLAUDE_CODE_GIT_BASH_PATH": "X:/my/bash.exe"})
            self.assertEqual(got[0], Path("X:/my/bash.exe"))
            self.assertIn(root / "bin" / "bash.exe", got)
            self.assertIn(root / "usr" / "bin" / "bash.exe", got)
            self.assertEqual(P.git_bash_candidates(exec_path, {})[0].name, "bash.exe")

    def test_shell_runs_command_on_this_os(self):
        self.assertEqual(subprocess.run(P.shell_argv("auto", "exit 3")).returncode, 3)


class ShimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_shim_files_posix_and_windows(self):
        posix = P.shim_files("/usr/bin/python3", windows=False)
        self.assertEqual(posix, {"artel": '#!/bin/sh\nexec "/usr/bin/python3" -m artel "$@"\n'})
        win = P.shim_files("C:\\Py\\python.exe", windows=True)
        self.assertEqual(win["artel"], '#!/bin/sh\nexec "C:\\Py\\python.exe" -m artel "$@"\n')
        self.assertEqual(win["artel.cmd"], '@"C:\\Py\\python.exe" -m artel %*\r\n')

    def test_wrapper_passes_arguments_verbatim(self):
        argv = P.write_wrapper(self.tmp / "wrap", py_cmd("import json, sys; print(json.dumps(sys.argv[1:]))"))
        args = ["a b", 'say "hi"', '{"k": [1, 2]}', "héllo", "plain"]
        out = subprocess.run(argv + args, capture_output=True, encoding="utf-8")
        self.assertEqual(json.loads(out.stdout), args)

    def test_written_shim_runs_artel(self):
        bin_dir = P.write_shim(self.tmp / "bin", sys.executable)
        out = subprocess.run(P.shim_invocation(bin_dir) + ["--version"], capture_output=True,
                             encoding="utf-8", env=subprocess_env(), cwd=REPO_ROOT)
        self.assertIn("0.1.0", out.stdout + out.stderr)


class MiscTest(unittest.TestCase):
    def test_which_finds_python(self):
        self.assertTrue(P.which(Path(sys.executable).name) or P.which("python3") or P.which("python"))

    def test_resolve_argv_missing_raises(self):
        with self.assertRaises(ArtelError):
            P.resolve_argv(["definitely-not-a-real-binary-xyz"])

    def test_free_bytes_positive(self):
        self.assertGreater(P.free_bytes(Path(tempfile.gettempdir())), 0)

    def test_long_paths_warning(self):
        self.assertIn("core.longpaths", P.long_paths_warning(lambda *a, **k: "", windows=True))
        self.assertIsNone(P.long_paths_warning(lambda *a, **k: "true", windows=True))
        self.assertIsNone(P.long_paths_warning(lambda *a, **k: "", windows=False))


if __name__ == "__main__":
    unittest.main()

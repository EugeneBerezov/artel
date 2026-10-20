import json
import os
import subprocess
import sys
import tempfile
import threading
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

STOPPER = ("import os, time\nfrom artel.platform import install_stop_handlers\n"
           "def cb():\n    print('stopped', flush=True)\n    os._exit(0)\ninstall_stop_handlers(cb)\n")

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

    def test_blocking_acquire_waits_for_release(self):
        holder = P.FileLock(self.path)
        holder.acquire()
        threading.Timer(0.3, holder.release).start()
        waiter = P.FileLock(self.path)
        self.addCleanup(waiter.release)
        start = time.monotonic()
        self.assertTrue(waiter.acquire(blocking=True))
        self.assertGreaterEqual(time.monotonic() - start, 0.2)

    def test_lock_freed_when_holder_dies(self):
        code = ("import sys\nfrom pathlib import Path\nfrom artel.platform import FileLock\n"
                "assert FileLock(Path(sys.argv[1])).acquire()\n")   # exits holding the lock
        subprocess.run([sys.executable, "-c", code, str(self.path)], env=subprocess_env(), cwd=REPO_ROOT,
                       check=True)
        lock = P.FileLock(self.path)
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquire())

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
    def spawn(self, code, **kwargs):
        proc = subprocess.Popen(py_cmd(code), stdout=subprocess.PIPE, encoding="utf-8",
                                **kwargs, **P.popen_kwargs())
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

    def test_terminate_tree_graceful_stop_is_fast(self):
        # mutation caught: terminate_tree not sending the soft stop (would wait the whole grace, then kill)
        code = STOPPER + "print('up', flush=True)\n[time.sleep(0.1) for _ in range(600)]\n"
        proc = self.spawn(code, env=subprocess_env(), cwd=REPO_ROOT)
        proc.stdout.readline()
        start = time.monotonic()
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=20.0))
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(proc.stdout.read().strip(), "stopped")

    def test_terminate_tree_unreaped_leader_is_not_a_full_grace_wait(self):
        proc = self.spawn("import time; time.sleep(60)")
        time.sleep(0.3)
        start = time.monotonic()
        P.terminate_tree(proc.pid, P.group_of(proc), grace=20.0)
        self.assertLess(time.monotonic() - start, 10)

    def test_terminate_tree_sweeps_children_of_gone_leader(self):
        # leader reaped, grandchild still in the group (spec §5: reconcile terminates groups)
        proc = self.spawn(ORPHAN)
        grandchild = int(proc.stdout.readline())
        proc.wait(timeout=10)
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=1.0))
        self.assertTrue(wait_dead(grandchild))

    def test_terminate_tree_sweeps_children_of_zombie_leader(self):
        proc = self.spawn(ORPHAN)
        grandchild = int(proc.stdout.readline())
        deadline = time.monotonic() + 5
        while P.process_fingerprint(proc.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)   # leader exited, not yet reaped
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=1.0))
        self.assertTrue(wait_dead(grandchild))

    def test_terminate_tree_false_when_nothing_left(self):
        proc = self.spawn("pass")
        proc.wait(timeout=10)
        self.assertFalse(P.terminate_tree(proc.pid, P.group_of(proc), grace=0.5))

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
        procs = []
        for _ in range(2):   # macOS fingerprints have 1 s resolution
            proc = subprocess.Popen(py_cmd("import time; time.sleep(30)"))
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            procs.append(proc)
            time.sleep(1.1)
        first, second = (P.process_fingerprint(p.pid) for p in procs)
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_fingerprint_none_for_exited_pid(self):
        proc = subprocess.Popen(py_cmd("pass"))
        proc.wait(timeout=10)
        self.assertIsNone(P.process_fingerprint(proc.pid))


class StopHandlerTest(unittest.TestCase):
    def test_soft_terminate_runs_callback(self):
        code = ("import os, sys, time\nfrom artel.platform import install_stop_handlers\n"
                "def cb():\n    print('stopped', flush=True)\n    os._exit(0)\n"
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
        self.assertEqual(P.shell_argv("bash", "x", windows=False)[1:], ["-c", "x"])
        self.assertEqual(P.shell_argv("sh", "x", windows=False)[1:], ["-c", "x"])
        self.assertEqual(P.shell_argv("pwsh", "x", windows=False),
                         ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "x"])
        self.assertEqual(P.shell_argv("cmd", "x", windows=True), ["cmd", "/d", "/s", "/c", "x"])
        with self.assertRaises(ArtelError):
            P.shell_argv("fish", "x", windows=False)
        with self.assertRaises(ArtelError):   # list2cmdline would mangle the quotes
            P.shell_argv("cmd", 'echo "a b"', windows=True)

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
            self.assertEqual(got, [Path("X:/my/bash.exe"), root / "bin" / "bash.exe",
                                   root / "usr" / "bin" / "bash.exe"])
            self.assertEqual(P.git_bash_candidates(exec_path, {}), got[1:])

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
        pct = P.shim_files("C:\\100%\\python.exe", windows=True)
        self.assertEqual(pct["artel.cmd"], '@"C:\\100%%\\python.exe" -m artel %*\r\n')

    def test_wrapper_passes_arguments_verbatim(self):
        argv = P.write_wrapper(self.tmp / "wrap", py_cmd("import json, sys; print(json.dumps(sys.argv[1:]))"))
        args = ["a b", 'say "hi"', '{"k": [1, 2]}', "héllo", "plain", "a & b", "x | y", "<z> >w",
                "50% off", "^caret ^", "$HOME `x`", "back\\slash"]
        out = subprocess.run(argv + args, capture_output=True, encoding="utf-8")
        self.assertEqual(json.loads(out.stdout), args)

    def test_written_shim_runs_artel(self):
        bin_dir = P.write_shim(self.tmp / "bin", sys.executable)
        out = subprocess.run(P.shim_invocation(bin_dir) + ["--version"], capture_output=True,
                             encoding="utf-8", env=subprocess_env(), cwd=REPO_ROOT)
        self.assertIn("0.1.0", out.stdout + out.stderr)


class OsPairTest(unittest.TestCase):
    @unittest.skipUnless(P.IS_WINDOWS, "Windows only")
    def test_windows_registers_job_and_fingerprint_is_filetime(self):
        proc = subprocess.Popen(py_cmd("import time; time.sleep(30)"), **P.popen_kwargs())
        self.addCleanup(proc.wait)
        P.register_tree(proc)
        self.addCleanup(lambda: P.hard_kill(proc.pid, P.group_of(proc)))
        self.assertIn(proc.pid, P._JOBS)
        self.assertTrue(P.tree_alive(proc.pid, P.group_of(proc)))
        self.assertTrue(P.process_fingerprint(proc.pid).isdigit())

    @unittest.skipIf(P.IS_WINDOWS, "POSIX only")
    def test_posix_has_no_jobs_and_group_is_pid(self):
        proc = subprocess.Popen(py_cmd("import time; time.sleep(30)"), **P.popen_kwargs())
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        P.register_tree(proc)
        self.assertEqual(P._JOBS, {})
        self.assertEqual(P.group_of(proc), proc.pid)


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

        def unset(*a, **k):
            raise ArtelError("exit 1")
        self.assertIn("core.longpaths", P.long_paths_warning(unset, windows=True))


if __name__ == "__main__":
    unittest.main()

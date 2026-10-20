import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from artel import platform as P
from artel.errors import ArtelError
from tests.helpers import IGNORE_SOFT_STOP, REPO_ROOT, py_cmd, subprocess_env

TREE = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
time.sleep(60)
"""

STOPPER = ("import time\nfrom artel import platform as P\ns = P.install_stop_handlers()\n"
           "def wait_stop():\n    while not s.requested():\n        time.sleep(0.05)\n"
           "    print('stopped', flush=True)\n    raise SystemExit(0)\n")

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
        """Mutation caught: a blocking acquire that returns False."""
        holder = P.FileLock(self.path)
        holder.acquire()
        threading.Timer(0.3, holder.release).start()
        waiter = P.FileLock(self.path)
        self.addCleanup(waiter.release)
        start = time.monotonic()
        self.assertTrue(waiter.acquire(blocking=True))
        self.assertGreaterEqual(time.monotonic() - start, 0.2)

    def test_lock_freed_when_holder_dies(self):
        """Mutation caught: a lock that outlives its holder process."""
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
        deadline = time.monotonic() + 10   # the Windows job's ActiveProcesses lags the exit by a moment
        while P.tree_alive(proc.pid, P.group_of(proc)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(P.tree_alive(proc.pid, P.group_of(proc)))

    def test_terminate_skips_on_fingerprint_mismatch(self):
        proc = self.spawn(TREE)
        proc.stdout.readline()
        self.assertFalse(P.terminate_tree(proc.pid, P.group_of(proc), grace=0.5, fingerprint="bogus"))
        self.assertIsNone(proc.poll())

    def test_terminate_tree_sends_soft_stop_first(self):
        """Mutation caught: terminate_tree not sending the soft stop (no 'stopped', waits the whole grace)."""
        code = STOPPER + "print('up', flush=True)\nwait_stop()\n"
        proc = self.spawn(code, env=subprocess_env(), cwd=REPO_ROOT)
        proc.stdout.readline()
        start = time.monotonic()
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=20.0))
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(proc.stdout.read().strip(), "stopped")
        proc.wait(timeout=10)

    def test_terminate_tree_returns_promptly_after_soft_stop(self):
        """Mutation caught: waiting the full grace for an unreaped (zombie) leader."""
        proc = self.spawn("import time; time.sleep(60)")
        time.sleep(0.3)
        start = time.monotonic()
        P.terminate_tree(proc.pid, P.group_of(proc), grace=20.0)
        self.assertLess(time.monotonic() - start, 10)

    @unittest.skipIf(P.IS_WINDOWS, "POSIX only")
    def test_zombie_leader_does_not_cut_children_grace(self):
        """Mutation caught: killing the group the moment the leader is a zombie (children get no grace)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            marker = Path(tmp) / "cleaned"
            kid = ("import signal, sys, time\n"
                   "def stop(*a):\n    time.sleep(0.6)\n"
                   f"    open({str(marker)!r}, 'w', encoding='utf-8').write('x')\n    sys.exit(0)\n"
                   "signal.signal(signal.SIGTERM, stop)\nprint('ready', flush=True)\n"
                   "while True:\n    time.sleep(0.1)\n")
            leader = ("import subprocess, sys\n"
                      f"kid = subprocess.Popen([sys.executable, '-c', {kid!r}], stdout=subprocess.PIPE,"
                      " encoding='utf-8')\n"
                      "kid.stdout.readline()\nprint('up', flush=True)\n"
                      "import time\nwhile True:\n    time.sleep(0.1)\n")
            proc = self.spawn(leader)
            proc.stdout.readline()
            P.terminate_tree(proc.pid, P.group_of(proc), grace=10.0)   # leader dies on SIGTERM, stays unreaped
            self.assertTrue(marker.exists())

    def test_terminate_tree_sweeps_group_after_leader_exit(self):
        """Mutation caught: 'leader gone -> False' leaving orphans that hold the pipes."""
        # leader reaped, grandchild still in the group (spec §5: reconcile terminates groups)
        proc = self.spawn(ORPHAN)
        grandchild = int(proc.stdout.readline())
        proc.wait(timeout=10)
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=1.0))
        self.assertTrue(wait_dead(grandchild))

    def test_terminate_tree_sweeps_children_of_zombie_leader(self):
        """Mutation caught: treating a zombie leader as 'nothing alive'."""
        proc = self.spawn(ORPHAN)
        grandchild = int(proc.stdout.readline())
        deadline = time.monotonic() + 5
        while P.process_fingerprint(proc.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)   # leader exited, not yet reaped
        self.assertTrue(P.terminate_tree(proc.pid, P.group_of(proc), grace=1.0))
        self.assertTrue(wait_dead(grandchild))

    def test_terminate_returns_false_when_nothing_alive(self):
        """Mutation caught: signalling a gone tree / returning True."""
        proc = self.spawn("pass")
        proc.wait(timeout=10)
        deadline = time.monotonic() + 10   # Windows: the job's ActiveProcesses lags the exit by a moment
        while P.tree_alive(proc.pid, P.group_of(proc)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(P.terminate_tree(proc.pid, P.group_of(proc), grace=0.5))

    def test_job_object_kills_orphaned_grandchild(self):
        """Mutation caught: hard_kill acting only on the leader."""
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
    def test_stop_handler_only_records(self):
        """Mutation caught: a handler that prints/sets an Event/starts a thread (reentrant-print CI failure)."""
        code = ("import time\nfrom artel import platform as P\ns = P.install_stop_handlers()\n"
                "print('up', flush=True)\n"
                "while not s.requested():\n    time.sleep(0.05)\nprint('stopped', s.count, flush=True)\n")
        proc = subprocess.Popen(py_cmd(code), stdout=subprocess.PIPE, encoding="utf-8",
                                env=subprocess_env(), cwd=REPO_ROOT, **P.popen_kwargs())
        P.register_tree(proc)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: P.hard_kill(proc.pid, P.group_of(proc)))
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        P.soft_terminate(proc.pid, P.group_of(proc))
        out = proc.stdout.read()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertIn("stopped 1", out)


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
        P.which, orig_which = (lambda name: "/opt/" + name), P.which
        try:   # catches swapping the sh/bash branch with the bare-name fallback
            self.assertEqual(P.shell_argv("sh", "x", windows=False), ["/opt/sh", "-c", "x"])
            self.assertEqual(P.shell_argv("bash", "x", windows=False), ["/opt/bash", "-c", "x"])
        finally:
            P.which = orig_which
        self.assertEqual(P.shell_argv("sh", "x", windows=False)[1:], ["-c", "x"])
        self.assertEqual(P.shell_argv("pwsh", "x", windows=False),
                         ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "x"])
        self.assertEqual(P.shell_argv("cmd", "x", windows=True), ["cmd", "/d", "/s", "/c", "x"])
        with self.assertRaises(ArtelError):
            P.shell_argv("fish", "x", windows=False)

    def test_shell_argv_bash_on_windows_uses_git_bash(self):
        orig = P.find_git_bash
        P.find_git_bash = lambda: "C:/Git/bin/bash.exe"
        try:
            self.assertEqual(P.shell_argv("bash", "x", windows=True), ["C:/Git/bin/bash.exe", "-c", "x"])
        finally:
            P.find_git_bash = orig

    def test_command_line_for_cmd(self):
        argv = P.shell_argv("cmd", 'echo "a b"', windows=True)
        self.assertEqual(P.command_line(argv, windows=True), 'cmd /d /s /c "echo "a b""')
        self.assertEqual(P.command_line(argv, windows=False), argv)
        self.assertEqual(P.command_line(["git", "log"], windows=True), ["git", "log"])

    @unittest.skipUnless(P.IS_WINDOWS, "Windows only")
    def test_cmd_shell_runs_quoted_command(self):
        """Mutation caught: list2cmdline quoting (\\") reaching cmd.exe."""
        out = subprocess.run(P.command_line(P.shell_argv("cmd", 'echo "a b"')), capture_output=True,
                             encoding="utf-8", errors="replace")
        self.assertIn('"a b"', out.stdout)

    @unittest.skipIf(P.IS_WINDOWS, "POSIX only")
    def test_sh_shell_runs_quoted_command(self):
        out = subprocess.run(P.command_line(P.shell_argv("sh", 'echo "a b"')), capture_output=True,
                             encoding="utf-8")
        self.assertEqual(out.stdout.strip(), "a b")

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

    def test_cmd_shim_text(self):
        """Mutation caught: ROOT spliced into the code string, `-I`/`-X utf8` dropped, unquoted or unescaped
        arguments, LF line endings, `%*` missing."""
        win = P.shim_files("C:\\Py\\python.exe", "C:\\100%\\src", windows=True)
        cmd = win["artel.cmd"]
        self.assertIn("chcp 65001 >nul", cmd)                      # PD-33 preamble
        self.assertIn('set "ARTEL_CP=%%a"', cmd)
        self.assertIn("chcp %ARTEL_CP% >nul", cmd)
        line = f'"C:\\Py\\python.exe" -X utf8 -I -c "{P.SHIM_CODE}" "C:\\100%%\\src" %*\r\n'
        self.assertIn(line, cmd)
        self.assertTrue(cmd.rstrip().endswith("exit /b %ARTEL_RC%"))
        self.assertLess(cmd.index("chcp 65001"), cmd.index("-X utf8"))
        self.assertNotIn("\n", cmd.replace("\r\n", ""))
        self.assertNotIn('"', P.SHIM_CODE)
        self.assertNotIn("%", P.SHIM_CODE)
        self.assertNotIn("100", P.SHIM_CODE)                       # the root is an argument, not part of the code

    def test_cmd_shim_quotes_code_with_double_quotes(self):
        """Mutation caught: single quotes around the code (cmd.exe does not honour them)."""
        cmd = P.shim_files("C:\\Py\\python.exe", "C:\\r", windows=True)["artel.cmd"]
        self.assertIn(f'-c "{P.SHIM_CODE}"', cmd)
        self.assertNotIn(f"-c '{P.SHIM_CODE}'", cmd)

    def test_sh_shim_text_and_git_bash_paths(self):
        """Mutation caught: Windows paths left with backslashes, or as /c/x (breaks under MSYS_NO_PATHCONV); ROOT inside the quoted code."""
        posix = P.shim_files("/usr/bin/python3", "/src/artel", windows=False)
        self.assertEqual(list(posix), ["artel"])
        self.assertEqual(posix["artel"], "#!/bin/sh\nexec /usr/bin/python3 -X utf8 -I -c "
                         f"'{P.SHIM_CODE}' /src/artel \"$@\"\n")
        win = P.shim_files("C:\\Py\\python.exe", "C:\\a b\\x", windows=True)["artel"]
        self.assertIn("exec C:/Py/python.exe -X utf8 -I -c", win)
        self.assertIn("'C:/a b/x' \"$@\"", win)
        unc = P.shim_files("\\\\h\\s\\python.exe", "\\\\h\\s\\src", windows=True)["artel"]
        self.assertIn("exec //h/s/python.exe", unc)
        self.assertIn(" //h/s/src ", unc)

    def test_shim_code_adds_root_only_when_not_on_sys_path(self):
        """Mutation caught: an unconditional insert(0) (site-packages ahead of the stdlib for a pip install)."""
        code = P.SHIM_CODE.replace("from artel.cli import main; raise SystemExit(main())", "print(sys.path[:2])")
        probe = lambda root, extra: subprocess.run(  # noqa: E731
            [sys.executable, "-I", "-c", code, root], capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONPATH": ""}).stdout
        self.assertTrue(probe("/fresh/root", "").startswith("['/fresh/root'"))
        prog = ("import sys, runpy; sys.path.append('/known'); sys.argv = ['-c', '/known']; "
                f"exec({code!r}.replace('print(sys.path[:2])', 'print(sys.path.count(\"/known\"), sys.path[0] == \"/known\")'))")
        out = subprocess.run([sys.executable, "-I", "-c", prog], capture_output=True, encoding="utf-8").stdout
        self.assertEqual(out.strip(), "1 False")
        self.assertNotIn('"', P.SHIM_CODE)
        self.assertNotIn("%", P.SHIM_CODE)

    def test_cmd_shim_root_ending_in_backslash(self):
        """Mutation caught: `"D:\\"` (a drive-root checkout): the backslash escapes the closing quote."""
        cmd = P.shim_files("C:\\py.exe", "D:\\", windows=True)["artel.cmd"]
        self.assertIn(f'"{P.SHIM_CODE}" "D:\\\\" %*', cmd)

    def test_write_shim_repairs_a_lost_exec_bit(self):
        if P.IS_WINDOWS:
            self.skipTest("no exec bit")
        bin_dir = P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        (bin_dir / "artel").chmod(0o644)
        P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        self.assertTrue(os.access(bin_dir / "artel", os.X_OK))

    def test_zipapp_shim_text(self):
        """Mutation caught: the source form (`-c ... artel.cli`) used for a .pyz."""
        sh = P.shim_files("/py", "/x/artel.pyz", zipapp=True, windows=False)["artel"]
        self.assertEqual(sh, '#!/bin/sh\nexec /py -X utf8 -I /x/artel.pyz "$@"\n')
        cmd = P.shim_files("C:\\py.exe", "C:\\x\\artel.pyz", zipapp=True, windows=True)["artel.cmd"]
        self.assertIn('"C:\\py.exe" -X utf8 -I "C:\\x\\artel.pyz" %*', cmd)
        self.assertNotIn("-c", cmd.split("chcp 65001 >nul")[1])

    def slot_with(self, **modules: str) -> Path:
        slot = self.tmp / "slot"
        (slot / "artel").mkdir(parents=True, exist_ok=True)
        (slot / "artel" / "__init__.py").write_text("", encoding="utf-8")
        (slot / "artel" / "cli.py").write_text("print('SLOT')\n", encoding="utf-8")
        for name, text in modules.items():
            (slot / name).write_text(text, encoding="utf-8")
        return slot

    def run_shim(self, slot: Path, *args: str, root: str = str(REPO_ROOT), **env_extra: str):
        shim = P.shim_invocation(P.write_shim(self.tmp / "bin", sys.executable, root))
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}   # the shim adds nothing itself
        env.update(PYTHONIOENCODING="utf-8", **env_extra)
        return subprocess.run(shim + list(args), cwd=slot, env=env, capture_output=True, encoding="utf-8",
                              errors="replace")

    def test_shim_runs_runner_artel_not_slot_package(self):
        """Mutation caught: `python -m artel` (the slot's artel/ package would run)."""
        res = self.run_shim(self.slot_with(), "--help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("SLOT", res.stdout + res.stderr)
        self.assertIn("usage", res.stdout.lower())

    def test_shim_slot_stdlib_shadow_is_ignored(self):
        """Mutation caught: dropping -I (the cwd would be on sys.path and argparse.py would run)."""
        slot = self.slot_with(**{"argparse.py": "print('SLOT')\nraise SystemExit(9)\n"})
        res = self.run_shim(slot, "--help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("SLOT", res.stdout + res.stderr)

    def test_shim_ignores_empty_pythonpath_entry(self):
        """Mutation caught: -P instead of -I (an empty PYTHONPATH entry means the cwd)."""
        slot = self.slot_with(**{"argparse.py": "print('SLOT')\nraise SystemExit(9)\n"})
        res = self.run_shim(slot, "--help", PYTHONPATH=os.pathsep)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("SLOT", res.stdout + res.stderr)

    def test_shim_ignores_pythonpath_pointing_at_slot(self):
        """Mutation caught: dropping both -I and -E (e.g. -P alone) while the slot is on PYTHONPATH."""
        slot = self.slot_with(**{"argparse.py": "print('SLOT')\nraise SystemExit(9)\n"})
        res = self.run_shim(slot, "--help", PYTHONPATH=str(slot))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("SLOT", res.stdout + res.stderr)

    def test_shim_root_with_special_characters(self):
        """Mutation caught: ROOT spliced into the code string, or double-quoted in sh."""
        name = "a b'c%d&e \u00fc" if P.IS_WINDOWS else "a b'c\"d$e"
        root = self.tmp / name
        shutil.copytree(REPO_ROOT / "artel", root / "artel", ignore=shutil.ignore_patterns("__pycache__"))
        res = self.run_shim(self.slot_with(), "--version", root=str(root))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "0.1.0")

    def test_shim_passes_arguments_and_exit_code(self):
        res = self.run_shim(self.slot_with(), "add")   # argparse error: missing title
        self.assertEqual(res.returncode, 2)
        self.assertIn("title", res.stderr)

    @unittest.skipUnless(P.IS_WINDOWS, "Git Bash on Windows")
    def test_shim_via_git_bash_on_windows(self):
        """Mutation caught: an sh form that only works with POSIX paths."""
        bin_dir = P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        res = subprocess.run([P.find_git_bash(), str(bin_dir / "artel"), "--version"], capture_output=True,
                             encoding="utf-8", cwd=self.tmp)
        self.assertEqual(res.stdout.strip(), "0.1.0", res.stderr)

    def test_shim_zipapp_form(self):
        """Mutation caught: the source form used for a .pyz."""
        pyz = self.tmp / "artel.pyz"
        built = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "build_pyz.py"), "--out", str(pyz),
                                "--staging", str(self.tmp / "stage")], cwd=self.tmp, capture_output=True,
                               encoding="utf-8", errors="replace")
        self.assertEqual(built.returncode, 0, built.stderr)
        shim = P.shim_invocation(P.write_shim(self.tmp / "bin", sys.executable, str(pyz), zipapp=True))
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        res = subprocess.run(shim + ["--version"], cwd=self.slot_with(), env=env, capture_output=True,
                             encoding="utf-8", errors="replace")
        self.assertEqual(res.stdout.strip(), "0.1.0", res.stderr)

    def test_write_shim_unchanged_content_not_rewritten(self):
        """Mutation caught: truncating/rewriting the file on every stage."""
        bin_dir = P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        files = [bin_dir / name for name in P.shim_files(sys.executable, str(REPO_ROOT))]
        for f in files:
            os.utime(f, ns=(10**18, 10**18))
        P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        self.assertEqual([f.stat().st_mtime_ns for f in files], [10**18] * len(files))
        P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT / "other"))   # changed content is written
        self.assertNotEqual(files[0].stat().st_mtime_ns, 10**18)

    def test_write_shim_replace_is_atomic_and_retries(self):
        """Mutation caught: writing in place; giving up on the first PermissionError."""
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) <= 2:
                raise PermissionError("held by cmd.exe")
            return real(src, dst)

        with mock.patch.object(P.os, "replace", flaky), mock.patch.object(P.time, "sleep"):
            bin_dir = P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual((bin_dir / "artel").read_text(encoding="utf-8"),
                         P.shim_files(sys.executable, str(REPO_ROOT))["artel"])
        self.assertEqual([p.name for p in bin_dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_write_shim_gives_up_after_five_permission_errors(self):
        with mock.patch.object(P.os, "replace", side_effect=PermissionError("x")) as rep, \
                mock.patch.object(P.time, "sleep"):
            with self.assertRaises(PermissionError):
                P.write_shim(self.tmp / "bin", sys.executable, str(REPO_ROOT))
        self.assertEqual(rep.call_count, 5)
        self.assertEqual([p.name for p in (self.tmp / "bin").iterdir()], [])   # no partial or temp file left

    def test_wrapper_passes_arguments_verbatim(self):
        argv = P.write_wrapper(self.tmp / "wrap", py_cmd("import json, sys; print(json.dumps(sys.argv[1:]))"))
        args = ["a b", 'say "hi"', '{"k": [1, 2]}', "héllo", "plain", "a & b", "x | y", "<z> >w", "a^ &b",
                "50% off", "^caret ^", "$HOME `x`", "back\\slash", "100%"]
        out = subprocess.run(argv + args, capture_output=True, encoding="utf-8")
        self.assertEqual(json.loads(out.stdout), args)

    def test_wrapper_in_non_ascii_percent_dir(self):
        """Mutation caught: UTF-8 .cmd read in the OEM code page; unescaped % in the baked path."""
        folder = self.tmp / "тест 100%"
        folder.mkdir()
        script = folder / "s.py"
        script.write_text("import sys; sys.exit(7)\n", encoding="utf-8", newline="\n")
        argv = P.write_wrapper(folder / "wrap", [sys.executable, str(script)])
        self.assertEqual(subprocess.run(argv).returncode, 7)


class OsPairTest(unittest.TestCase):
    @unittest.skipIf(P.IS_WINDOWS, "POSIX only")
    def test_pid_alive_false_for_unreaped_zombie(self):
        """Mutation caught: dropping the zombie check."""
        proc = subprocess.Popen(py_cmd("pass"))
        self.addCleanup(proc.wait)
        deadline = time.monotonic() + 10
        while P.process_fingerprint(proc.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(P.pid_alive(proc.pid))

    @unittest.skipUnless(P.IS_WINDOWS, "Windows only")
    def test_pid_alive_access_denied_is_alive(self):
        """Mutation caught: ERROR_ACCESS_DENIED read as dead."""
        self.assertTrue(P.pid_alive(4))   # the System process: OpenProcess is denied

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_fingerprint_shape_linux(self):
        self.assertTrue(P.process_fingerprint(os.getpid()).isdigit())

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_fingerprint_shape_macos(self):
        self.assertRegex(P.process_fingerprint(os.getpid()), r"\d{1,2}:\d{2}:\d{2}")

    @unittest.skipUnless(P.IS_WINDOWS, "Windows only")
    def test_fingerprint_shape_windows(self):
        self.assertTrue(P.process_fingerprint(os.getpid()).isdigit())

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
        def fake(value):
            def repo_git(*args, **kwargs):
                self.assertEqual(args, ("config", "--type=bool", "core.longpaths"))
                return value
            return repo_git
        self.assertIn("core.longpaths", P.long_paths_warning(fake(""), windows=True))
        self.assertIsNone(P.long_paths_warning(fake("true"), windows=True))
        self.assertIsNone(P.long_paths_warning(lambda *a, **k: "", windows=False))

        def unset(*a, **k):
            raise ArtelError("exit 1")
        self.assertIn("core.longpaths", P.long_paths_warning(unset, windows=True))


if __name__ == "__main__":
    unittest.main()

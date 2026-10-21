import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from artel import config as C, testguard
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION, Repo, task_branch
from artel.protect import Hit
from tests.helpers import RepoTestCase, git

UNITTEST_LINES = "test_a (tests.t.A.test_a) ... ok\ntest_b (tests.t.A.test_b) ... ok\n"
DEFAULT_RX = ("^(\\w+ \\([\\w.]+\\))(?:\\r?\\n[^\\r\\n]*)? \\.\\.\\. ok\\r?$", "^PASSED (\\S+)")


class GuardCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Repo.discover(self.repo_path)
        self.n = 0

    def cfg(self, **cli):
        return C.load_config(self.repo, env={}, cli={k: (v, "test") for k, v in cli.items()})

    def integration_with(self, files):
        for rel, content in files.items():
            self.commit_file(rel, content, f"base {rel}")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")

    def task(self, files=(), *, delete=(), rename=(), binary=None):
        """A task branch from the integration tip with files written (a dict), deleted or renamed."""
        self.n += 1
        path = self.tmp / f"task-{self.n}"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(self.n), str(path), INTEGRATION)
        for rel, content in dict(files).items():
            self.commit_file(rel, content, f"task {rel}", cwd=path)
        for rel, data in (binary or {}).items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_bytes(data)
            git(path, "add", rel)
            git(path, "commit", "-q", "-m", f"binary {rel}")
        for rel in delete:
            git(path, "rm", "-q", rel)
            git(path, "commit", "-q", "-m", f"delete {rel}")
        for old, new in rename:
            (path / new).parent.mkdir(parents=True, exist_ok=True)
            git(path, "mv", old, new)
            git(path, "commit", "-q", "-m", f"rename {old}")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        return task_branch(self.n)

    def hard(self, tip, **cli):
        return testguard.hard_hits(self.repo, self.cfg(**cli), tip)

    def soft(self, tip, **cli):
        return testguard.soft_hits(self.repo, self.cfg(**cli), tip)


TESTS = "import unittest\n\nclass A(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"


class DiffHitsTest(GuardCase):
    def test_pure_test_addition_is_free(self):
        """Mutation caught: counting additions (a new test file or appended lines) as a change."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_new.py": TESTS, "tests/test_old.py": TESTS + "\n    def test_b(self):\n        pass\n"})
        self.assertEqual((self.hard(tip), self.soft(tip)), ([], []))

    def test_changed_line_is_soft_only(self):
        """Mutation caught: the round-2 rule that sent every edited test line (71 % of merges) to a human."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_old.py": TESTS.replace("import unittest", "import unittest as ut")})
        self.assertEqual(self.hard(tip), [])
        self.assertEqual(self.soft(tip), [Hit("tests/test_old.py", "test_paths", "1 lines changed")])

    def test_fewer_assertions_is_hard(self):
        """Mutation caught: comparing totals instead of removed minus added assertion lines."""
        old = "def test_a(self):\n    self.assertEqual(1, 1)\n    self.assertEqual(2, 2)\n    self.assertTrue(x)\n"
        self.integration_with({"tests/test_old.py": old})
        tip = self.task({"tests/test_old.py": "def test_a(self):\n    self.assertTrue(x)\n    self.assertEqual(3, 3)\n"})
        self.assertEqual(self.hard(tip), [Hit("tests/test_old.py", "test_assert_markers", "1 fewer assertion lines")])

    def test_replaced_assertion_is_soft(self):
        """Mutation caught: any removed assertion line counting as a hard hit."""
        self.integration_with({"tests/test_old.py": "def test_a(self):\n    self.assertEqual(1, 1)\n"})
        tip = self.task({"tests/test_old.py": "def test_a(self):\n    self.assertEqual(1, 2 - 1)\n"})
        self.assertEqual(self.hard(tip), [])
        self.assertEqual([h.note for h in self.soft(tip)], ["1 lines changed"])

    def test_deleted_test_file_is_hard(self):
        """Mutation caught: a deleted test file only escalating the review."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task(delete=["tests/test_old.py"])
        self.assertEqual(self.hard(tip), [Hit("tests/test_old.py", "test_paths", "deleted")])
        self.assertEqual(self.soft(tip), [])

    def test_renamed_test_file_is_hard(self):
        """Mutation caught: rename detection hiding the delete (a moved test is a deleted test under its old
        path; `--no-renames` shows D + A)."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task(rename=[("tests/test_old.py", "tests/test_moved.py")])
        self.assertEqual(self.hard(tip), [Hit("tests/test_old.py", "test_paths", "deleted")])

    def test_markers_hard(self):
        """Mutation caught: a marker missing from the check (each disables a test or ends the process)."""
        self.integration_with({"tests/test_old.py": TESTS})
        cases = ["@unittest.skip('x')\n", "@pytest.mark.skip\n", "it.only('x', f)\n", "@pytest.mark.xfail\n",
                 "os._exit(0)\n", "@unittest.skip\n", "@unittest.skip ('x')\n", "@skip ('x')\n",
                 "pytestmark = pytest.mark.skip\n", 'pytestmark = [pytest.mark.skip(reason="x")]\n',
                 '@unittest.skipIf(True, "x")\n', '@unittest.skipUnless(False, "x")\n', "xfail(reason)\n",
                 "xit('x', f)\n", "@unittest.expectedFailure\n", "@mark.skip\n", "@pytest.mark.skipif(True, reason='x')\n"]
        for line in cases:
            with self.subTest(line=line):
                tip = self.task({"tests/test_old.py": TESTS + line})
                hits = self.hard(tip)
                self.assertEqual([(h.path, h.source) for h in hits], [("tests/test_old.py", "test_markers")])
                self.assertIn(line.strip(), hits[0].note)

    def test_marker_hit_is_once_per_regex_and_clipped(self):
        """Mutation caught: one hit per added line (a long file of skips floods the VERDICT) or an unbounded
        line in the note."""
        self.integration_with({"tests/test_old.py": TESTS})
        text = TESTS + "".join(f"@unittest.skip('{'x' * 80}{i}')\n" for i in range(3))
        hits = self.hard(self.task({"tests/test_old.py": text}))
        self.assertEqual(len(hits), 1)
        self.assertLessEqual(len(hits[0].note), len('adds ""') + 60)

    def test_marker_once_per_path(self):
        """Mutation caught: the once-only set kept across paths (a second file's skip would go unreported)."""
        self.integration_with({"tests/test_a.py": TESTS, "tests/test_b.py": TESTS})
        tip = self.task({"tests/test_a.py": TESTS + "os._exit(0)\n", "tests/test_b.py": TESTS + "os._exit(0)\n"})
        self.assertEqual([h.path for h in self.hard(tip)], ["tests/test_a.py", "tests/test_b.py"])

    def test_conditional_skip_and_sys_exit_are_not_markers(self):
        """Mutation caught: the round-2 marker list (skipIf, sys.exit and `fit(` are ordinary test code)."""
        self.integration_with({"tests/test_old.py": TESTS})
        extra = ('@unittest.skipIf(sys.platform == "win32", "posix")\n'
                 'cmd = py_cmd("import sys; sys.exit(3)")\nmodel.fit(x)\n'
                 "pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
                 '    self.skipTest("no x")\n    pytest.skip("no x")\n@unittest.skipUnless(HAS_X, "x")\n'
                 "@unittest.skipIf(HAS_X, 'x')\n@skip_if_slow\n")
        self.assertEqual(self.hard(self.task({"tests/test_old.py": TESTS + extra})), [])

    def test_test_setup_change_is_soft(self):
        """Mutation caught: a conftest or `__init__` change (additions included) merging with a cheap review."""
        self.integration_with({"tests/__init__.py": "", "tests/test_old.py": TESTS})
        tip = self.task({"tests/__init__.py": "import os\n", "conftest.py": "import pytest\n"})
        self.assertEqual(self.hard(tip), [])
        self.assertEqual([(h.path, h.note) for h in self.soft(tip)],
                         [("conftest.py", "test setup changed"), ("tests/__init__.py", "test setup changed")])

    def test_binary_change_is_soft_unless_added(self):
        """Mutation caught: a binary fixture rewrite going unseen (no text lines to count), or a new binary
        fixture counted as a change."""
        self.integration_with({"tests/test_old.py": TESTS})
        git(self.repo_path, "checkout", "-q", "-b", "side")
        (self.repo_path / "tests" / "test_blob.py").write_bytes(b"\x00\x01\x02")
        git(self.repo_path, "add", "tests/test_blob.py")
        git(self.repo_path, "commit", "-q", "-m", "blob")
        git(self.repo_path, "checkout", "-q", "main")
        git(self.repo_path, "merge", "-q", "--ff-only", "side")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        changed = self.task(binary={"tests/test_blob.py": b"\x00\x03\x04"})
        self.assertEqual([h.note for h in self.soft(changed)], ["binary change"])
        added = self.task(binary={"tests/test_new_blob.py": b"\x00\x05"})
        self.assertEqual(self.soft(added), [])

    def test_removed_marker_is_not_a_hit(self):
        """Mutation caught: markers looked for on removed lines too (deleting a skip decorator is a fix)."""
        self.integration_with({"tests/test_old.py": TESTS + "@unittest.skip('x')\nos._exit(0)\n"})
        tip = self.task({"tests/test_old.py": TESTS})
        self.assertEqual(self.hard(tip), [])

    def test_load_tests_in_test_file_is_soft(self):
        """Mutation caught: a `load_tests` hook (it can drop whole suites) merging after a cheap review."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_old.py": TESTS + "\ndef load_tests(loader, tests, pattern):\n    return tests\n"})
        self.assertEqual(self.hard(tip), [])
        self.assertEqual(self.soft(tip), [Hit("tests/test_old.py", "test_paths", "adds load_tests")])

    def test_many_files_cost_two_git_diffs(self):
        """Mutation caught: one `git diff` per path (a 1000-file test codemod is thousands of processes)."""
        files = {f"tests/test_{i}.py": TESTS for i in range(7)}
        self.integration_with(files)
        tip = self.task({rel: TESTS + "x = 1\n" for rel in files})
        calls = []
        real = Repo.git_bytes

        def counting(repo, *args, **kw):
            calls.append(args)
            return real(repo, *args, **kw)
        with mock.patch.object(Repo, "git_bytes", counting):
            scanned = testguard.scan(self.repo, self.cfg(), tip)
            testguard.hard_hits(self.repo, self.cfg(), tip, scanned)
            testguard.soft_hits(self.repo, self.cfg(), tip, scanned)
        self.assertEqual(len([a for a in calls if "diff" in a]), 2)   # --raw once, -U0 once; hits share the scan
        self.assertEqual(len(scanned.changes), 7)

    def test_chunked_diff_keeps_each_path_with_its_lines(self):
        """Mutation caught: sections matched to the wrong path at a chunk boundary."""
        files = {f"tests/test_{i}.py": TESTS for i in range(5)}
        self.integration_with(files)
        tip = self.task({rel: TESTS.replace("1, 1", f"{i}, 9") if i in (1, 4) else TESTS + "x = 1\n"
                         for i, rel in enumerate(files)})
        with mock.patch.object(testguard, "_CHUNK", 2):
            self.assertEqual([h.path for h in self.soft(tip)], ["tests/test_1.py", "tests/test_4.py"])

    def test_section_count_mismatch_falls_back_to_per_path_diffs(self):
        """Mutation caught: trusting the order of sections when git printed a different number than there are
        paths (a type change prints two): lines would be credited to the wrong files."""
        files = {f"tests/test_{i}.py": TESTS for i in range(3)}
        self.integration_with(files)
        tip = self.task({"tests/test_0.py": TESTS + "x = 1\n", "tests/test_1.py": TESTS.replace("1, 1", "1, 9"),
                         "tests/test_2.py": TESTS + "x = 1\n"})
        real, calls = testguard._sections, []

        def lying(text):
            calls.append(1)
            found = real(text)
            return found[:-1] if len(calls) == 1 else found   # the first, whole-chunk parse comes back short
        with mock.patch.object(testguard, "_sections", lying):
            self.assertEqual([h.path for h in self.soft(tip)], ["tests/test_1.py"])
        self.assertEqual(len(calls), 4)   # the chunk, then one diff per path

    def blob(self, text):
        return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.repo_path, input=text, check=True,
                              capture_output=True, encoding="utf-8").stdout.strip()

    def test_type_change_keeps_every_line_of_the_path(self):
        """Mutation caught: only the first of a type change's two sections kept (the markers added by the new
        file vanish). The index is written directly: no symlink is created on disk."""
        self.integration_with({"tests/test_link.py": TESTS})
        git(self.repo_path, "update-index", "--cacheinfo", f"120000,{self.blob('target')},tests/test_link.py")
        git(self.repo_path, "commit", "-q", "-m", "make it a link")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        git(self.repo_path, "update-index", "--cacheinfo", f"100644,{self.blob(TESTS + 'os._exit(0)')},tests/test_link.py")
        git(self.repo_path, "commit", "-q", "-m", "back to a file with a marker")
        git(self.repo_path, "branch", "-f", task_branch(1), "main")
        git(self.repo_path, "reset", "-q", "--hard", INTEGRATION)
        self.assertEqual([h.source for h in self.hard(task_branch(1))], ["test_markers"])

    def test_submodule_log_setting_does_not_break_the_scan(self):
        """Mutation caught: `diff.submodule=log` printing a section without a `diff --git` header (a gitlink
        bumped under tests/), which made the section count wrong and the fallback raise."""
        self.integration_with({"tests/test_old.py": TESTS})
        git(self.repo_path, "config", "diff.submodule", "log")
        git(self.repo_path, "checkout", "-q", "-b", "side", INTEGRATION)
        git(self.repo_path, "update-index", "--add", "--cacheinfo", f"160000,{'3' * 40},tests/fixture")
        self.commit_file("tests/test_old.py", TESTS + "os._exit(0)\n", "marker")
        git(self.repo_path, "branch", "-f", task_branch(1), "side")
        git(self.repo_path, "checkout", "-q", "main")
        self.assertEqual([h.source for h in self.hard(task_branch(1))], ["test_markers"])

    def test_surrogates_never_reach_a_hit(self):
        """Mutation caught: a latin-1 line or a non-UTF-8 name read with surrogate escapes into a Hit, where the
        journal's utf-8 encode raised (every verify of the task then crashed)."""
        self.integration_with({"tests/test_old.py": TESTS})
        path = self.tmp / "latin"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(1), str(path), INTEGRATION)
        (path / "tests" / "test_old.py").write_bytes(TESTS.encode("utf-8") + b"@unittest.skip('caf\xe9')\n")
        git(path, "commit", "-q", "-am", "latin-1 skip")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        (hit,) = self.hard(task_branch(1))
        self.assertEqual(hit.source, "test_markers")
        hit.note.encode("utf-8")   # raises on a lone surrogate
        hit.path.encode("utf-8")

    def test_non_utf8_path_round_trips(self):
        """Mutation caught: a path git prints that is not UTF-8 decoded lossily, so its own diff comes back
        empty and the assertion and marker checks silently pass (Linux file systems only)."""
        self.integration_with({})
        try:   # decoding is inside the try: Windows refuses the bytes already here
            name = os.fsdecode(b"tests/test_\xff.py")
            (self.repo_path / "tests").mkdir()
            (self.repo_path / name).write_text(TESTS, encoding="utf-8", newline="\n")
        except (OSError, UnicodeError):
            self.skipTest("this file system cannot hold a non-UTF-8 name")
        git(self.repo_path, "add", "-A")
        git(self.repo_path, "commit", "-q", "-m", "odd name")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        path = self.tmp / "odd-task"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(9), str(path), INTEGRATION)
        (path / name).write_text("def test_a(self):\n    pass\nos._exit(0)\n", encoding="utf-8", newline="\n")
        git(path, "commit", "-q", "-am", "gut it")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        hits = self.hard(task_branch(9))
        self.assertEqual({h.source for h in hits}, {"test_assert_markers", "test_markers"})
        for hit in hits:
            hit.path.encode("utf-8")   # shown with U+FFFD, never a lone surrogate

    def test_lone_cr_cannot_hide_marker(self):
        """Mutation caught: the diff read in text mode (a lone CR became a newline or hid what follows it), or
        markers and assertions matched against the whole line only. A CR followed by `diff --git` must not open
        a fake section either: the lines after it still belong to their own file."""
        self.integration_with({"tests/test_a.py": TESTS + "assert a\rassert b\n", "tests/test_b.py": TESTS,
                               "tests/test_c.py": TESTS})
        path = self.tmp / "cr"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(1), str(path), INTEGRATION)
        (path / "tests" / "test_a.py").write_bytes(TESTS.encode("utf-8") + b"x = 1\r@unittest.skip('x')\r"
                                                   b"diff --git a/tests/test_b.py b/tests/test_b.py\n")
        (path / "tests" / "test_b.py").write_bytes(TESTS.encode("utf-8") + b"os._exit(0)\n")
        (path / "tests" / "test_c.py").write_bytes(TESTS.encode("utf-8") + b"x = 1\rdef load_tests(a, b, c):\r")
        git(path, "commit", "-q", "-am", "cr")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        hits = {(h.path, h.source, h.note) for h in self.hard(task_branch(1))}
        self.assertEqual({(p, s) for p, s, _ in hits},
                         {("tests/test_a.py", "test_markers"), ("tests/test_a.py", "test_assert_markers"),
                          ("tests/test_b.py", "test_markers")})
        self.assertIn(("tests/test_a.py", "test_assert_markers", "2 fewer assertion lines"), hits)
        self.assertEqual([h.note for h in self.soft(task_branch(1)) if h.path == "tests/test_c.py"],
                         ["adds load_tests"])   # also behind a lone CR

    def test_cr_in_a_path_does_not_hide_changes(self):
        """Mutation caught: `--raw -z` read in text mode (a CR inside a name turned into a newline, the path no
        longer matched `test_paths` and its markers were never looked at). Windows cannot hold such a name."""
        name = "tests/test_\rb.py"
        self.integration_with({})
        try:
            (self.repo_path / "tests").mkdir()
            (self.repo_path / name).write_text(TESTS, encoding="utf-8", newline="\n")
        except (OSError, ValueError):
            self.skipTest("this file system cannot hold a CR in a name")
        git(self.repo_path, "add", "-A")
        git(self.repo_path, "commit", "-q", "-m", "cr name")
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        path = self.tmp / "cr-name"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(5), str(path), INTEGRATION)
        (path / name).write_text(TESTS + "@unittest.skip('x')\n", encoding="utf-8", newline="\n")
        git(path, "commit", "-q", "-am", "skip it")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        (hit,) = self.hard(task_branch(5))
        self.assertEqual(hit.source, "test_markers")
        self.assertNotIn("\r", hit.path)   # shown without the control character

    def test_marker_in_conftest_is_hard(self):
        """Mutation caught: markers looked for in test files only (a matching file that is not a test file, such
        as conftest.py, can disable tests too)."""
        self.integration_with({"conftest.py": "import pytest\n"})
        tip = self.task({"conftest.py": "import pytest\npytestmark = pytest.mark.skip\n"})
        self.assertEqual([(h.path, h.source) for h in self.hard(tip)], [("conftest.py", "test_markers")])

    def test_chunks_are_bounded_by_length_and_count(self):
        """Mutation caught: only the path count bounding a command line (long paths overflow Windows' 32 767)."""
        long = ["d" * 3000 + str(i) for i in range(7)]
        groups = testguard._chunks(long)
        self.assertEqual([p for g in groups for p in g], long)
        self.assertTrue(all(sum(len(p) + 1 for p in g) <= testguard._ARGV_CHARS for g in groups))
        self.assertGreater(len(groups), 2)
        short = [f"t{i}" for i in range(250)]
        self.assertEqual([len(g) for g in testguard._chunks(short)], [100, 100, 50])
        self.assertEqual(testguard._chunks([]), [])
        self.assertEqual(testguard._chunks(["x" * 9000]), [["x" * 9000]])   # one path is never dropped

    def test_windows_command_line_error_fails_closed(self):
        """Mutation caught: Windows reporting a too-long command line as FileNotFoundError, which `_run_bytes`
        turns into `git not found`: that ArtelError must not escape `scan` into the runner."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_old.py": TESTS + "x = 1\n"})
        with mock.patch.object(Repo, "git_bytes", side_effect=ArtelError("git not found on PATH")):
            (hit,) = self.hard(tip)
        self.assertEqual(hit.source, "test_paths")

    def test_oserror_spawning_git_fails_closed(self):
        """Mutation caught: an OSError (command line too long, no process slots) escaping `scan` into the
        runner, or reading as `no changes`."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_old.py": TESTS + "x = 1\n"})
        with mock.patch.object(Repo, "git_bytes", side_effect=OSError("[WinError 206] too long")):
            (hit,) = self.hard(tip)
        self.assertEqual((hit.path, hit.source), ("<git error>", "test_paths"))
        self.assertIn("too long", hit.note)

    def test_marker_outside_test_paths_ignored(self):
        """Mutation caught: scanning every changed file (a `@skip` in product code is not a disabled test)."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"src/app.py": "@unittest.skip('x')\nos._exit(0)\nassert x\n"})
        self.assertEqual((self.hard(tip), self.soft(tip)), ([], []))

    def test_glob_entry_matches_file_name(self):
        """Mutation caught: a glob entry matched against the whole path (`*_check.py` never matches `x/a_check.py`)
        or case-sensitively."""
        self.integration_with({"x/a_check.py": "assert 1\n"})
        tip = self.task(delete=["x/a_check.py"])
        self.assertEqual(self.hard(tip, test_paths='["*_CHECK.py"]'),
                         [Hit("x/a_check.py", "test_paths", "deleted")])
        self.assertEqual(self.hard(tip, test_paths='["x_*.py"]'), [])
        self.assertTrue(testguard.path_matches("a/B/Test_X.PY", "test_*.py"))
        self.assertFalse(testguard.path_matches("test_dir/x.py", "test_*"))   # the name only, not a component

    def test_odd_path_parsing(self):
        """Mutation caught: splitting git's output on whitespace, or decoding with the locale (`-z` keeps a path
        with a space and non-ASCII letters whole)."""
        self.integration_with({"tests/a b/tést.py": TESTS})
        tip = self.task(delete=["tests/a b/tést.py"])
        self.assertEqual(self.hard(tip), [Hit("tests/a b/tést.py", "test_paths", "deleted")])
        edited = self.task({"tests/a b/tést.py": TESTS.replace("1, 1", "1, 2")})
        self.assertEqual([h.path for h in self.soft(edited)], ["tests/a b/tést.py"])

    def test_commented_assertion_is_a_removal(self):
        """Mutation caught: counting a commented-out assertion as still there (N6)."""
        self.integration_with({"tests/test_old.py": "def test_a(self):\n    self.assertEqual(a, b)\n"})
        tip = self.task({"tests/test_old.py": "def test_a(self):\n    # self.assertEqual(a, b)\n"})
        self.assertEqual(self.hard(tip), [Hit("tests/test_old.py", "test_assert_markers", "1 fewer assertion lines")])
        for comment in ("// expect(a).toBe(b)", "/* assert a */", " * assert a", "# assert a"):
            with self.subTest(comment=comment):   # every comment lead counts on neither side
                self.integration_with({"tests/test_c.py": "x = 1\n" + comment + "\n"})
                same = self.task({"tests/test_c.py": "x = 2\n" + comment + "\n"})
                self.assertEqual(self.hard(same), [])   # a removed and re-added comment is not an assertion change
                gone = self.task({"tests/test_c.py": "x = 1\n"})
                self.assertEqual(self.hard(gone), [])   # a removed comment line is no removed assertion

    def test_xfail_mentioned_in_text_is_not_a_marker(self):
        """Mutation caught: a bare `xfail` substring marker (a comment or a docstring about xfail)."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task({"tests/test_old.py": TESTS + "# see xfail docs\nNOTE = 'xfail is a pytest word'\n"})
        self.assertEqual(self.hard(tip), [])

    def test_added_triple_plus_line_is_content(self):
        """Mutation caught: skipping every `+++` line as a header (an added `+++ assert` line would vanish from
        the count)."""
        self.integration_with({"tests/test_old.py": "x = 1\n"})
        tip = self.task({"tests/test_old.py": "x = 1\n++ os._exit(0)\n"})
        self.assertEqual([h.source for h in self.hard(tip)], ["test_markers"])

    def test_empty_test_paths_disables_the_diff_checks(self):
        """Mutation caught: an empty list matching everything."""
        self.integration_with({"tests/test_old.py": TESTS})
        tip = self.task(delete=["tests/test_old.py"])
        self.assertEqual((self.hard(tip, test_paths="[]"), self.soft(tip, test_paths="[]")), ([], []))

    def test_git_failure_is_a_hard_hit(self):
        """Mutation caught: fail-open (a tip git cannot diff passing as unchanged)."""
        self.integration_with({"tests/test_old.py": TESTS})
        (hit,) = self.hard("0" * 40)
        self.assertEqual((hit.path, hit.source), ("<git error>", "test_paths"))
        self.assertEqual(self.soft("0" * 40), [])

    def test_is_test_file(self):
        """Mutation caught: `conftest.py` or `__init__.py` taken for test files (they are setup)."""
        for path in ("tests/test_a.py", "a_test.go", "x/b.test.js", "c.spec.ts", "TEST_UP.py"):
            self.assertTrue(testguard.is_test_file(path), path)
        for path in ("tests/__init__.py", "conftest.py", "tests/data.json", "tests/helpers.py", "tox.ini"):
            self.assertFalse(testguard.is_test_file(path), path)


class IdsTest(unittest.TestCase):
    def test_passed_ids_crlf(self):
        """Mutation caught: a `$`-only default regex that fails on a log with CRLF endings (N4)."""
        crlf = UNITTEST_LINES.replace("\n", "\r\n")
        self.assertEqual(testguard.passed_ids(crlf, DEFAULT_RX), testguard.passed_ids(UNITTEST_LINES, DEFAULT_RX))
        self.assertEqual(len(testguard.passed_ids(crlf, DEFAULT_RX)), 2)

    def test_passed_ids_unittest_v_and_pytest_ra(self):
        """Mutation caught: counting skipped or failed tests as passed, a docstring line hiding a test, a missing
        pytest pattern, `None` and an empty set confused."""
        code = textwrap.dedent('''
            import unittest

            class A(unittest.TestCase):
                def test_doc(self):
                    """Docstring line."""

                @unittest.skip("later")
                def test_skipped(self):
                    pass

                def test_plain(self):
                    pass

            unittest.main(module=__name__, argv=["x", "-v"])
        ''')
        run = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8")
        ids = testguard.passed_ids(run.stdout + run.stderr, DEFAULT_RX)
        self.assertEqual({i.split()[0] for i in ids}, {"test_doc", "test_plain"})
        self.assertFalse(any("test_skipped" in i for i in ids))
        self.assertEqual(testguard.passed_ids("PASSED tests/t.py::test_x\nFAILED tests/t.py::test_y\n", DEFAULT_RX),
                         frozenset({"tests/t.py::test_x"}))
        self.assertIsNone(testguard.passed_ids("nothing here\n", DEFAULT_RX))
        self.assertIsNone(testguard.passed_ids(UNITTEST_LINES, ()))


class BaseRecordTest(GuardCase):
    def setUp(self):
        super().setUp()
        from artel import board as B
        self.board = B.open_board(self.repo.board_path)
        self.addCleanup(self.board.close)
        self.sha = git(self.repo_path, "rev-parse", "HEAD")

    def test_write_base_round_trip(self):
        """Mutation caught: ids written for a red result, or the meta record missing a field."""
        ids = frozenset({"a", "b"})
        testguard.write_base(self.board, self.repo, sha=self.sha, green=True, at=5.0, log="logs/x.log", ids=ids)
        self.assertEqual(testguard.read_base(self.board), {"sha": self.sha, "green": True, "at": 5.0, "log": "logs/x.log"})
        self.assertEqual(testguard.base_ids(self.repo, self.sha), ids)
        other = "1" * 40
        testguard.write_base(self.board, self.repo, sha=other, green=False, at=6.0, log="", ids=ids)
        self.assertIsNone(testguard.base_ids(self.repo, other))

    def test_ids_files_keep_the_newest_twenty(self):
        """Mutation caught: an unbounded directory, or the pruning deleting the file just written."""
        for i in range(25):
            testguard.write_ids(self.repo, f"{i:040x}", frozenset({f"t{i}"}))
        files = list((self.repo.artel_dir / "verify-ids").glob("*.txt"))
        self.assertEqual(len(files), testguard.KEEP_IDS)
        self.assertEqual(testguard.base_ids(self.repo, f"{24:040x}"), frozenset({"t24"}))

    def test_pruning_keeps_the_integration_tip(self):
        """Mutation caught: the tip's file pruned once enough task verifies were written after it (the files are
        aged explicitly: equal mtimes and sort order must not hide the bug)."""
        tip = git(self.repo_path, "rev-parse", "HEAD")
        git(self.repo_path, "branch", "-f", INTEGRATION, tip)
        testguard.write_ids(self.repo, tip, frozenset({"keep"}))
        old = self.repo.artel_dir / "verify-ids" / f"{tip}.txt"
        os.utime(old, ns=(1_000_000_000, 1_000_000_000))   # the oldest file of all
        for i in range(30):
            testguard.write_ids(self.repo, f"{i + 1:040x}", frozenset({"x"}))
        self.assertEqual(testguard.base_ids(self.repo, tip), frozenset({"keep"}))
        self.assertEqual(len(list(old.parent.glob("*.txt"))), testguard.KEEP_IDS)

    def test_prune_keeps_newest(self):
        """Mutation caught: the pruning order reversed (the newest files deleted, the oldest kept)."""
        for i in range(25):
            sha = f"{i + 1:040x}"
            testguard.write_ids(self.repo, sha, frozenset({f"t{i}"}))
            stamp = 1_000_000_000 + i * 10
            os.utime(self.repo.artel_dir / "verify-ids" / f"{sha}.txt", ns=(stamp * 10**9, stamp * 10**9))
        kept = sorted(p.stem for p in (self.repo.artel_dir / "verify-ids").glob("*.txt"))
        self.assertEqual(kept, [f"{i + 1:040x}" for i in range(5, 25)])   # files 0-4, the oldest, are gone

    def test_existing_ids_file_never_replaced(self):
        """Mutation caught: a second green record shrinking a sha's baseline (a verify that merged nothing)."""
        sha = "2" * 40
        testguard.write_ids(self.repo, sha, frozenset({"a", "b"}))
        testguard.write_ids(self.repo, sha, frozenset({"a"}))
        testguard.write_base(self.board, self.repo, sha=sha, green=True, at=1.0, log="", ids=frozenset({"z"}))
        self.assertEqual(testguard.base_ids(self.repo, sha), frozenset({"a", "b"}))

    def test_read_base_ignores_garbage(self):
        """Mutation caught: a corrupt meta value crashing the runner instead of reading as `no record`."""
        self.assertIsNone(testguard.read_base(self.board))
        self.board.set_meta("base_verify", "{not json")
        self.assertIsNone(testguard.read_base(self.board))
        self.board.set_meta("base_verify", "[1]")
        self.assertIsNone(testguard.read_base(self.board))

    def test_ids_path_refuses_a_non_sha(self):
        """Mutation caught: a branch name or `../x` used as a file name."""
        testguard.write_ids(self.repo, "../evil", frozenset({"a"}))
        self.assertIsNone(testguard.base_ids(self.repo, "../evil"))


class RequiredTest(unittest.TestCase):
    def test_required_tests_parsed(self):
        """Mutation caught: case-sensitive prefix, no stripping, duplicates kept, order lost."""
        body = ("Do it.\nTests: test_b, test_a\n  \nTests:  test_c ,test_a,\r\nTests:\ttest_d,\ttest_e\t\r\n"
                "tests: not_a_requirement\nTESTS: neither\nnot Tests: here\n")
        self.assertEqual(testguard.required_tests(body), ["test_b", "test_a", "test_c", "test_d", "test_e"])

    def test_required_tests_plan_style(self):
        """Mutation caught: the plan's own style (`Tests:` with backticks and a remark) taken literally, so every
        such task asks; `tests: none` becoming a requirement."""
        body = 'Tests: `test_a` (the happy path), "test_b"\nTests: `test_c` (catches x, y), \u2014, `t.py::test_d[1]`\n'
        self.assertEqual(testguard.required_tests(body), ["test_a", "test_b", "test_c", "t.py::test_d[1]"])
        self.assertEqual(testguard.required_ignored(body), ["\u2014"])
        self.assertEqual(testguard.required_tests("tests: none\n"), [])

    def test_required_tests_bulleted_and_indented(self):
        """Mutation caught: a `Tests:` line in a markdown list or an indented block ignored (the check fails open)."""
        body = "Steps:\n- Tests: test_a\n  * Tests: test_b, test_c\n    Tests: test_d\n\tTests: test_e\n\t- Tests: test_f\n"
        self.assertEqual(testguard.required_tests(body), ["test_a", "test_b", "test_c", "test_d", "test_e", "test_f"])
        self.assertEqual(testguard.required_tests("see Tests: test_x\n"), [])

    def test_required_name_matches_parametrized_id(self):
        """Mutation caught: a pytest parameter suffix making a passed test look missing (fails closed)."""
        self.assertEqual(testguard.missing_required(["test_x", "test_y"], {"t.py::test_x[1]", "a.B.test_y[a-b]"}), [])
        self.assertEqual(testguard.missing_required(["test"], {"t.py::test_x[1]"}), ["test"])
        self.assertEqual(testguard.missing_required(names=["test_z"], passed_ids={"t.py::test_x"}), ["test_z"])
        self.assertEqual(testguard.missing_required(["test_x"], {"t.py::test_x[[1, 2]]", "t.py::test_y[a"}), [])
        self.assertEqual(testguard.missing_required(["test_y"], {"t.py::test_y[a"}), [])   # `PASSED (\S+)` cut it

    def test_required_tests_none_and_punctuation(self):
        """Mutation caught: `Tests: none`, `n/a` or a bare dash becoming a requirement nobody can meet."""
        for line in ("none", "None.", "N/A", "n/a:", "-", "...", "\u2014"):
            with self.subTest(line=line):
                self.assertEqual(testguard.required_tests(f"Tests: {line}\n"), [])
                self.assertEqual(len(testguard.required_ignored(f"Tests: {line}\n")), 1)
        self.assertEqual(testguard.required_tests("Tests: none, test_a\n"), ["test_a"])
        self.assertEqual(testguard.required_tests("Tests: nonexistent_ok\n"), ["nonexistent_ok"])
        self.assertEqual(testguard.required_tests("Tests: - test_a, * test_b\n"), ["test_a", "test_b"])
        self.assertEqual(testguard.required_tests("Intro\rTests: test_c\r"), ["test_c"])

    def test_class_name_does_not_satisfy_required(self):
        """Mutation caught: the bare class of an old-format unittest id (`test_x (pkg.mod.Retry)`) counting as a
        passed test named `Retry` or `pkg.mod.Retry`."""
        old = frozenset({"test_x (pkg.mod.Retry)"})
        self.assertEqual(testguard.missing_required(["Retry", "pkg.mod.Retry"], old), ["Retry", "pkg.mod.Retry"])
        self.assertEqual(testguard.missing_required(["test_x", "Retry.test_x", "pkg.mod.Retry.test_x"], old), [])

    def test_required_item_punctuation_and_nested_remarks(self):
        """Mutation caught: sentence punctuation or a nested remark leaving an unmeetable name (a false ask)."""
        self.assertEqual(testguard.required_tests("Tests: test_a, test_b.\n"), ["test_a", "test_b"])
        self.assertEqual(testguard.required_tests("Tests: test_a: the happy path\n"), ["test_a"])
        self.assertEqual(testguard.required_tests("Tests: test_a (catches f(x), not g), test_c\n"),
                         ["test_a", "test_c"])

    def test_no_tests_line_no_check(self):
        """Mutation caught: a task without a `Tests:` line getting a check."""
        self.assertEqual(testguard.required_tests("plain text\nTestsuite: no\n"), [])
        self.assertEqual(testguard.required_hits([], frozenset({"a"})), [])
        self.assertEqual(testguard.required_hits([], None), [])

    def test_required_name_matches_suffix_only_at_boundary(self):
        """Mutation caught: a plain `endswith` or substring match (`retry` satisfied by `test_retry`)."""
        self.assertEqual(testguard.missing_required(["retry"], {"a.B.test_retry"}), ["retry"])
        self.assertEqual(testguard.missing_required(["test_retry"], {"a.B.test_retry"}), [])
        self.assertEqual(testguard.missing_required(["test_retry"], {"t.py::test_retry"}), [])
        self.assertEqual(testguard.missing_required(["test_retry"], {"test_retry"}), [])
        self.assertEqual(testguard.missing_required(["test_retry"], {"a.B.xtest_retry"}), ["test_retry"])

    def test_unittest_id_matches_by_its_dotted_part(self):
        """Mutation caught: ids read by the default regex (`test_x (pkg.mod.Case.test_x)`) never satisfying a
        name, which would make every unittest project's required tests a hard hit."""
        ids = frozenset({"test_retry (tests.test_net.Retry.test_retry)"})
        self.assertEqual(testguard.missing_required(["test_retry", "Retry.test_retry", "test_other"], ids),
                         ["test_other"])

    def test_old_python_unittest_id_matches_class_dot_name(self):
        """Mutation caught: Python < 3.11 ids (`test_x (pkg.mod.Case)`, no method part) never matching a name."""
        ids = frozenset({"test_retry (tests.test_net.Retry)"})
        self.assertEqual(testguard.missing_required(["test_retry", "Retry.test_retry", "other"], ids), ["other"])

    def test_required_hits_are_one_listing(self):
        """Mutation caught: one hit per name (three names would fill the whole VERDICT)."""
        (hit,) = testguard.required_hits(["a", "b", "c", "d", "e"], frozenset({"z"}))
        self.assertEqual(hit.note, "required tests did not pass: a, b, c +2 more")

    def test_required_without_ids_is_hard(self):
        """Mutation caught: nothing to compare against passing silently."""
        for ids in (None, frozenset()):
            (hit,) = testguard.required_hits(["a"], ids)
            self.assertEqual((hit.path, hit.source), (testguard.VERIFY, "verify_test_id_regex"))
            self.assertIn("printed no test ids", hit.note)


if __name__ == "__main__":
    unittest.main()

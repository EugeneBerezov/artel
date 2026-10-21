import subprocess
import sys
import textwrap
import unittest

from artel import config as C, testguard
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
                 "os._exit(0)\n"]
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
                 'cmd = py_cmd("import sys; sys.exit(3)")\nmodel.fit(x)\n')
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
        body = "Do it.\nTests: test_b, test_a\n  \ntests:  test_c ,test_a,\nTESTS:x\nnot Tests: here\n"
        self.assertEqual(testguard.required_tests(body), ["test_b", "test_a", "test_c", "x"])

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

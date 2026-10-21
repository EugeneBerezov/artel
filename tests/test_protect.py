import sys
import unittest
from pathlib import Path

from artel import config as C, protect
from artel.errors import GitError
from artel.gitutil import INTEGRATION, Repo, task_branch
from artel.protect import Entry, Hit
from tests.helpers import RepoTestCase, git


def entry(path, exact=False, source="protected_paths"):
    return Entry(path, exact, source)


class MatchesTest(unittest.TestCase):
    def test_matches_component_anywhere(self):
        """Mutation caught: substring matching (`CLAUDE.md.bak`) or matching only at the root."""
        e = entry("CLAUDE.md")
        for path in ("CLAUDE.md", "docs/CLAUDE.md"):
            self.assertTrue(protect.matches(path, e), path)
        self.assertFalse(protect.matches("CLAUDE.md.bak", e))
        self.assertFalse(protect.matches("docs/MYCLAUDE.md", e))
        d = entry(".claude/")
        for path in (".claude/settings.json", "a/.claude/x", "a/b/.claude/c/d"):
            self.assertTrue(protect.matches(path, d), path)
        self.assertFalse(protect.matches("claude/settings.json", d))

    def test_matches_anchored_prefix(self):
        """Mutation caught: an entry with an inner `/` matched anywhere, or as a string prefix."""
        e = entry("scripts/verify.sh")
        self.assertTrue(protect.matches("scripts/verify.sh", e))
        self.assertFalse(protect.matches("x/scripts/verify.sh", e))
        self.assertFalse(protect.matches("scripts/verify.sh2", e))
        d = entry("tools/ci/")
        self.assertTrue(protect.matches("tools/ci/run.sh", d))
        self.assertFalse(protect.matches("tools/cinema/run.sh", d))
        self.assertFalse(protect.matches("a/tools/ci/run.sh", d))

    def test_matches_casefold(self):
        """Mutation caught: a case-sensitive compare (macOS and Windows file systems ignore case)."""
        self.assertTrue(protect.matches("claude.MD", entry("CLAUDE.md")))
        self.assertTrue(protect.matches("Scripts/Verify.sh", entry("scripts/verify.sh")))
        self.assertTrue(protect.matches("Tools/Check.py", entry("tools/check.py", exact=True)))

    def test_backslashes_read_as_separators(self):
        """Mutation caught: Windows spellings of a path never matching."""
        self.assertTrue(protect.matches("docs\\CLAUDE.md", entry("CLAUDE.md")))
        self.assertTrue(protect.matches("scripts/verify.sh", entry("scripts\\verify.sh")))

    def test_derived_entry_is_exact(self):
        """Mutation caught: prefix or component matching of a derived entry."""
        e = entry("tools/check.py", exact=True, source="commands.verify")
        self.assertTrue(protect.matches("tools/check.py", e))
        for path in ("tools/check.py.bak", "tools/other.py", "x/tools/check.py", "tools/check.py/x"):
            self.assertFalse(protect.matches(path, e), path)

    def test_summary_caps_at_three(self):
        """Mutation caught: an unbounded list in a journal entry, or a wrong count."""
        make = lambda n: [Hit(f"p{i}", "protected_paths") for i in range(n)]   # noqa: E731
        self.assertEqual(protect.summary(make(1)), "p0")
        self.assertEqual(protect.summary(make(3)), "p0, p1, p2")
        self.assertEqual(protect.summary(make(5)), "p0, p1, p2 +2 more")


class RepoCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)

    def cfg(self, **cli):
        return C.load_config(self.repo, env={}, cli={k: (v, "test") for k, v in cli.items()})

    def on_integration(self, rel, content="i\n"):
        path = self.tmp / f"int-{len(list(self.tmp.iterdir()))}"
        git(self.repo_path, "worktree", "add", "-q", str(path), INTEGRATION)
        self.commit_file(rel, content, f"integration {rel}", cwd=path)
        git(self.repo_path, "worktree", "remove", "--force", str(path))

    def branch_with(self, tid, files, *, delete=(), rename=()):
        """Task branch from the integration tip with the given files added, some deleted or renamed."""
        path = self.tmp / f"task-{tid}"
        git(self.repo_path, "worktree", "add", "-q", "-b", task_branch(tid), str(path), INTEGRATION)
        for rel, content in files.items():
            self.commit_file(rel, content, f"task {rel}", cwd=path)
        for rel in delete:
            git(path, "rm", "-q", rel)
            git(path, "commit", "-q", "-m", f"delete {rel}")
        for old, new in rename:
            (path / new).parent.mkdir(parents=True, exist_ok=True)
            git(path, "mv", old, new)
            git(path, "commit", "-q", "-m", f"rename {old}")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        return task_branch(tid)


class EntriesTest(RepoCase):
    def sources(self, cfg):
        return {(e.path, e.exact, e.source) for e in protect.entries(self.repo, cfg)}

    def test_entries_derive_list_form_verify_files(self):
        """Mutation caught: parsing a string command, adding flags or missing files as entries."""
        self.on_integration("tools/check.py")
        cfg = self.cfg(**{"commands.verify": [sys.executable, "tools/check.py", "--fast", "nope.py"]})
        derived = {e for e in self.sources(cfg) if e[2] == "commands.verify"}
        self.assertEqual(derived, {("tools/check.py", True, "commands.verify")})
        string_form = self.cfg(**{"commands.verify": f"{sys.executable} tools/check.py"})
        self.assertFalse({e for e in self.sources(string_form) if e[2] == "commands.verify"})

    def test_setup_files_are_derived_too(self):
        """Mutation caught: only verify consulted (setup runs on the runner's machine as well)."""
        self.on_integration("tools/setup.py")
        cfg = self.cfg(**{"commands.setup": [sys.executable, "tools/setup.py"]})
        self.assertIn(("tools/setup.py", True, "commands.setup"), self.sources(cfg))

    def test_directory_argument_protects_nothing(self):
        """Mutation caught: `git cat-file -e`, which accepts directories (a whole tests/ tree would be protected)."""
        self.on_integration("tests/test_x.py")
        cfg = self.cfg(**{"commands.verify": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]})
        self.assertFalse({e for e in self.sources(cfg) if e[2] == "commands.verify"})
        self.branch_with(1, {"tests/test_x.py": "changed\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)), [])

    def test_absolute_and_dotted_arguments_are_made_relative(self):
        """Mutation caught: `./tools/check.py` or an absolute path inside the repository escaping protection;
        a path outside the repository or with `..` adding an entry."""
        self.on_integration("tools/check.py")
        cfg = self.cfg(**{"commands.verify": [
            sys.executable, "./tools/check.py", str(self.repo.root / "tools" / "check.py"),
            "../tools/check.py", "/etc/passwd"]})
        self.assertEqual({e for e in self.sources(cfg) if e[2] == "commands.verify"},
                         {("tools/check.py", True, "commands.verify")})

    def test_empty_setting_keeps_derived_entries(self):
        """Mutation caught: an empty protected_paths switching derived entries off too."""
        self.on_integration("tools/check.py")
        cfg = self.cfg(protected_paths="[]", **{"commands.verify": [sys.executable, "tools/check.py"],
                                                 "commands.setup_inputs": ["lock.txt"]})
        self.assertEqual(self.sources(cfg), {("tools/check.py", True, "commands.verify"),
                                             ("lock.txt", True, "commands.setup_inputs")})

    def test_entries_include_templates_dir(self):
        """Mutation caught: the templates directory missing (a prompt edit would auto-merge)."""
        cfg = self.cfg(templates_dir="my/prompts")
        self.assertIn(("my/prompts", False, "templates_dir"), self.sources(cfg))

    def test_setup_inputs_are_exact_entries(self):
        """Mutation caught: a component match (`x/package-lock.json` protected) or setup_inputs ignored."""
        cfg = self.cfg(protected_paths="[]", **{"commands.setup_inputs": ["pkg/install.sh"]})
        self.branch_with(1, {"pkg/install.sh": "a\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)),
                         [Hit("pkg/install.sh", "commands.setup_inputs")])
        self.branch_with(2, {"x/pkg/install.sh": "a\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(2)), [])

    def test_setup_input_lockfile_not_matched_in_a_subdirectory(self):
        """Mutation caught: a derived entry matched as a component (`x/package-lock.json` protected)."""
        cfg = self.cfg(protected_paths="[]", **{"commands.setup_inputs": ["package-lock.json"]})
        self.branch_with(1, {"package-lock.json": "{}\n"})
        self.assertEqual([h.path for h in protect.hits(self.repo, cfg, task_branch(1))], ["package-lock.json"])
        self.branch_with(2, {"x/package-lock.json": "{}\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(2)), [])

    def test_relative_spellings_are_normalized(self):
        """Mutation caught: `tools/../tools/check.py` or a differently cased absolute path escaping protection."""
        self.on_integration("tools/check.py")
        upper = str(self.repo.root).upper() + "/tools/check.py"
        upper = upper if Path(upper).exists() else None   # only a case-insensitive file system resolves it
        args = [sys.executable, "tools/x/../check.py"] + ([upper] if upper else [])
        cfg = self.cfg(**{"commands.verify": args})
        self.assertEqual({e.path for e in protect.entries(self.repo, cfg) if e.source == "commands.verify"},
                         {"tools/check.py"})

    def test_absolute_templates_dir_matches(self):
        """Mutation caught: an absolute templates_dir becoming an entry whose components never match."""
        cfg = self.cfg(templates_dir=str(self.repo.root / "my" / "prompts"))
        self.assertIn(("my/prompts", False, "templates_dir"), self.sources(cfg))
        self.branch_with(1, {"my/prompts/review.md": "t\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)), [Hit("my/prompts/review.md", "templates_dir")])

    def test_absolute_path_case_insensitive(self):
        """Mutation caught: dropping casefold from the repository-root prefix of an absolute argument."""
        self.on_integration("tools/check.py")
        shouted = str(self.repo.root).upper() + "/tools/check.py"
        cfg = self.cfg(**{"commands.verify": [sys.executable, shouted]})
        self.assertIn(("tools/check.py", True, "commands.verify"), self.sources(cfg))

    def test_dot_components_ignored(self):
        """Mutation caught: `./scripts/./verify.sh` escaping an entry `scripts/verify.sh`."""
        self.assertTrue(protect.matches("./scripts/./verify.sh", entry("scripts/verify.sh")))

    def test_dotdot_outside_repo_ignored(self):
        """Mutation caught: `../x/verify.sh` (outside the repository) becoming an entry."""
        self.on_integration("x/verify.sh")
        cfg = self.cfg(**{"commands.verify": [sys.executable, "../x/verify.sh", "a/../../x/verify.sh"]})
        self.assertFalse({e for e in self.sources(cfg) if e[2] == "commands.verify"})

    def test_entries_are_sorted_and_deduplicated(self):
        """Mutation caught: duplicates (one path hit twice) or an unstable order."""
        cfg = self.cfg(protected_paths='["b", "a", "a"]')
        found = [e.path for e in protect.entries(self.repo, cfg) if e.source == "protected_paths"]
        self.assertEqual(found, ["a", "b"])


class HitsTest(RepoCase):
    def test_changed_paths_counts_both_sides_of_rename_and_deletions(self):
        """Mutation caught: `-M` rename collapsing (the old protected path would vanish from the list)."""
        self.on_integration("CLAUDE.md", "rules\n")
        self.on_integration("old/name.txt", "x\n")
        self.branch_with(1, {}, delete=["CLAUDE.md"], rename=[("old/name.txt", "new/name.txt")])
        self.assertEqual(protect.changed_paths(self.repo, task_branch(1)),
                         ["CLAUDE.md", "new/name.txt", "old/name.txt"])

    def test_changed_paths_is_the_branch_side_only(self):
        """Mutation caught: a two-dot diff (a change made on integration after the branch point looks like the task's)."""
        self.branch_with(1, {"mine.txt": "a\n"})
        self.on_integration("theirs.txt")
        self.assertEqual(protect.changed_paths(self.repo, task_branch(1)), ["mine.txt"])

    def test_git_error_counts_as_protected_hit(self):
        """Mutation caught: fail-open (a git failure leaving the task unprotected)."""
        (hit,) = protect.hits(self.repo, self.cfg(), "artel/999")
        self.assertEqual((hit.path, hit.source), ("<git error>", "protected_paths"))
        self.assertTrue(hit.note.startswith("git failed while looking for protected paths: "))
        self.assertGreater(len(hit.note), len("git failed while looking for protected paths: "))   # git's own line
        with self.assertRaises(GitError):
            protect.changed_paths(self.repo, "artel/999")

    def test_is_blob_kinds_and_fail_closed(self):
        """Mutation caught: a submodule path read as a git error (every task protected), a directory read as a
        file, or a git failure read as 'not a file' (fail open)."""
        self.on_integration("dir/f.txt")
        path = self.tmp / "gl"
        git(self.repo_path, "worktree", "add", "-q", "-b", "gitlink", str(path), INTEGRATION)
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/lib")
        git(path, "commit", "-q", "-m", "gitlink")
        git(self.repo_path, "worktree", "remove", "--force", str(path))
        self.assertTrue(protect._is_blob(self.repo, "gitlink", "dir/f.txt"))
        self.assertFalse(protect._is_blob(self.repo, "gitlink", "dir"))
        self.assertFalse(protect._is_blob(self.repo, "gitlink", "vendor/lib"))
        self.assertFalse(protect._is_blob(self.repo, "gitlink", "missing.txt"))
        with self.assertRaises(GitError):
            protect._is_blob(self.repo, "no-such-branch", "dir/f.txt")

    def test_pathspec_magic_in_a_verify_argument_is_no_error(self):
        """Mutation caught: an argument like ':!vendor' read as a pathspec (git 128, every task flagged)."""
        self.assertFalse(protect._is_blob(self.repo, INTEGRATION, ":!vendor"))
        self.assertFalse(protect._is_blob(self.repo, INTEGRATION, ":(glob)scripts/*"))
        self.branch_with(1, {"src/app.py": "x\n"})
        cfg = self.cfg(**{"commands.verify": [sys.executable, "-c", "pass", ":!vendor"]})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)), [])

    def test_git_error_inside_entries_fails_closed(self):
        """Mutation caught: entries() evaluated outside the try (a failing _is_blob raises out of hits)."""
        from unittest import mock
        with mock.patch.object(protect, "_is_blob", side_effect=GitError(["ls-tree"], "fatal: boom\n")):
            (hit,) = protect.hits(self.repo, self.cfg(**{"commands.verify": [sys.executable, "tools/c.py"]}),
                                  INTEGRATION)
        self.assertEqual(hit.path, "<git error>")
        self.assertIn("fatal: boom", hit.note)

    def test_changed_paths_sorted(self):
        """Mutation caught: an unsorted list (a summary whose order changes between runs)."""
        self.branch_with(1, {"z.txt": "z\n", "a.txt": "a\n", "m/n.txt": "m\n"})
        self.assertEqual(protect.changed_paths(self.repo, task_branch(1)), ["a.txt", "m/n.txt", "z.txt"])

    def test_hits_empty_when_setting_empty_and_no_commands(self):
        """Mutation caught: a built-in entry surviving an empty setting."""
        cfg = self.cfg(protected_paths="[]")
        self.branch_with(1, {"CLAUDE.md": "x\n", ".github/w.yml": "x\n"})
        self.assertEqual(protect.entries(self.repo, cfg), ())
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)), [])

    def test_hits_with_empty_setting_uses_derived_entries(self):
        """Mutation caught: `hits` returning early on an empty setting."""
        self.on_integration("tools/check.py")
        cfg = self.cfg(protected_paths="[]", **{"commands.verify": [sys.executable, "tools/check.py"]})
        self.branch_with(1, {"tools/check.py": "changed\n", "CLAUDE.md": "x\n"})
        self.assertEqual(protect.hits(self.repo, cfg, task_branch(1)), [Hit("tools/check.py", "commands.verify")])

    def test_reason_names_the_source_setting(self):
        """Mutation caught: every hit blamed on protected_paths (the user would pull the wrong lever)."""
        self.on_integration("tools/check.py")
        cfg = self.cfg(**{"commands.verify": [sys.executable, "tools/check.py"]})
        self.branch_with(1, {"tools/check.py": "c\n", "CLAUDE.md": "x\n", ".artel/templates/review.md": "t\n"})
        self.assertEqual(
            {(h.path, h.source) for h in protect.hits(self.repo, cfg, task_branch(1))},
            {("tools/check.py", "commands.verify"), ("CLAUDE.md", "protected_paths"),
             (".artel/templates/review.md", "protected_paths")})

    def test_paths_with_spaces_and_non_ascii_are_matched(self):
        """Mutation caught: dropping `-z` (git quotes `"docs/\\303\\251/CLAUDE.md"` and the match is lost)."""
        self.branch_with(1, {"docs/é dir/CLAUDE.md": "x\n", "w eird/.claude/s.json": "{}\n"})
        self.assertEqual({h.path for h in protect.hits(self.repo, self.cfg(), task_branch(1))},
                         {"docs/é dir/CLAUDE.md", "w eird/.claude/s.json"})

    def test_unprotected_change_has_no_hits(self):
        """Mutation caught: every change reported as protected."""
        self.branch_with(1, {"src/app.py": "x\n", "docs/readme.md": "y\n"})
        self.assertEqual(protect.hits(self.repo, self.cfg(), task_branch(1)), [])


if __name__ == "__main__":
    unittest.main()

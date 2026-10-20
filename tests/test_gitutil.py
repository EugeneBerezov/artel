import os
from pathlib import Path

from artel import gitutil
from artel.errors import ArtelError, GitError
from tests.helpers import RepoTestCase, git
import tempfile
from unittest import mock


class GitutilTest(RepoTestCase):
    def test_discover_from_subdir(self):
        sub = self.repo_path / "a" / "b"
        sub.mkdir(parents=True)
        repo = gitutil.Repo.discover(sub)
        self.assertEqual(repo.root.resolve(), self.repo_path)
        self.assertTrue(repo.common_dir.is_absolute())
        self.assertEqual(repo.tmp_dir, repo.artel_dir / "tmp")
        self.assertEqual(repo.stop_path, repo.artel_dir / "stop")
        self.assertEqual(repo.board_path.name, "board.db")

    def test_discover_from_linked_worktree(self):
        wt = self.tmp / "wt"
        git(self.repo_path, "worktree", "add", "-q", "-b", "x", str(wt))
        a = gitutil.Repo.discover(wt)
        b = gitutil.Repo.discover(self.repo_path)
        self.assertEqual(a.root.resolve(), self.repo_path)
        self.assertEqual(a.common_dir.resolve(), b.common_dir.resolve())

    def test_discover_outside_repo_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            with self.assertRaises(ArtelError) as cm:
                gitutil.Repo.discover(Path(d))
        self.assertIn("not inside a git repository", str(cm.exception))

    def test_rev_and_is_ancestor(self):
        repo = gitutil.Repo.discover(self.repo_path)
        first = repo.rev("HEAD")
        second = self.commit_file("f", "x\n")
        self.assertEqual(repo.rev("main"), second)
        self.assertIsNone(repo.rev("nope"))
        self.assertTrue(repo.is_ancestor(first, second))
        self.assertFalse(repo.is_ancestor(second, first))
        self.assertTrue(repo.branch_exists("main"))
        self.assertFalse(repo.branch_exists("nope"))

    def test_worktrees_lists_branch_and_detached(self):
        wt = self.tmp / "wt"
        git(self.repo_path, "worktree", "add", "-q", "-b", "x", str(wt))
        det = self.tmp / "det"
        git(self.repo_path, "worktree", "add", "-q", "--detach", str(det))
        repo = gitutil.Repo.discover(self.repo_path)
        by_path = {w.path.resolve(): w for w in repo.worktrees()}
        self.assertEqual(by_path[self.repo_path].branch, "main")
        self.assertEqual(by_path[wt.resolve()].branch, "x")
        self.assertIsNone(by_path[det.resolve()].branch)
        self.assertEqual(by_path[det.resolve()].head, repo.rev("HEAD"))

    def test_git_error_carries_stderr(self):
        repo = gitutil.Repo.discover(self.repo_path)
        with self.assertRaises(GitError) as cm:
            repo.git("checkout", "no-such-branch")
        self.assertTrue(cm.exception.stderr)
        self.assertEqual(cm.exception.args_list[0], "checkout")
        self.assertNotEqual(repo.git("checkout", "no-such-branch", check=False).returncode, 0)

    def test_task_branch_name(self):
        self.assertEqual(gitutil.task_branch(7), "artel/7")
        self.assertEqual(gitutil.INTEGRATION, "artel/integration")


class GitutilMoreTest(RepoTestCase):
    def repo(self):
        return gitutil.Repo.discover(self.repo_path)

    def test_paths_under_artel_dir(self):
        """Mutation caught: any of the pinned artel paths pointing elsewhere."""
        repo = self.repo()
        base = repo.common_dir / "artel"
        self.assertEqual(repo.artel_dir, base)
        self.assertEqual(repo.lock_path, base / "runner.lock")
        self.assertEqual(repo.logs_dir, base / "logs")
        self.assertEqual(repo.board_path, base / "board.db")
        self.assertEqual(repo.stop_path, base / "stop")
        self.assertEqual(repo.tmp_dir, base / "tmp")

    def test_git_passes_cwd_input_env(self):
        """Mutation caught: Repo.git ignoring cwd=, input= or env=."""
        repo = self.repo()
        blob = repo.git("hash-object", "--stdin", input="x").stdout.strip()
        self.assertEqual(blob, "c1b0730e0133447badcfd47fd144e254807b06e1")   # git's id of the bytes "x"
        wt = self.tmp / "wt"
        git(self.repo_path, "worktree", "add", "-q", "-b", "other", str(wt))
        self.commit_file("w.txt", "w\n", "in worktree", cwd=wt)
        self.assertEqual(repo.git("rev-parse", "HEAD", cwd=wt).stdout.strip(), git(wt, "rev-parse", "HEAD"))
        self.assertNotEqual(git(wt, "rev-parse", "HEAD"), git(self.repo_path, "rev-parse", "HEAD"))
        env = dict(os.environ, GIT_AUTHOR_NAME="Someone Else")
        repo.git("commit", "--allow-empty", "-q", "-m", "e", env=env)
        self.assertEqual(git(self.repo_path, "log", "-1", "--format=%an"), "Someone Else")

    def test_rev_peels_to_commit(self):
        """Mutation caught: rev() returning the tag or tree object instead of a commit / None."""
        repo = self.repo()
        head = git(self.repo_path, "rev-parse", "HEAD")
        git(self.repo_path, "tag", "-a", "-m", "annotated", "v-test")
        self.assertNotEqual(git(self.repo_path, "rev-parse", "v-test"), head)   # the tag object
        self.assertEqual(repo.rev("v-test"), head)
        self.assertIsNone(repo.rev(git(self.repo_path, "rev-parse", "HEAD^{tree}")))

    def test_git_missing_raises_artel_error(self):
        """Mutation caught: a missing git reported as 'not inside a git repository' / a leaked FileNotFoundError."""
        repo = self.repo()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with self.assertRaises(ArtelError) as cm:
                gitutil.Repo.discover(self.repo_path)
            self.assertIn("git not found on PATH", str(cm.exception))
            with self.assertRaises(ArtelError) as cm:
                repo.git("status")
            self.assertIn("git not found on PATH", str(cm.exception))

from pathlib import Path

from artel import gitutil
from artel.errors import ArtelError, GitError
from tests.helpers import RepoTestCase, git
import tempfile


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

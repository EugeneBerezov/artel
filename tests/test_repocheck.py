import unittest
from pathlib import Path

from artel import board as B, config as C, repocheck, slots
from artel.gitutil import INTEGRATION, Repo
from tests.helpers import RepoTestCase, git


class RepoCheckCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)
        self.board = B.open_board(self.repo.board_path)
        self.addCleanup(self.board.close)
        self.cfg = C.load_config(self.repo, env={}, cli={})
        slots.ensure_slots(self.repo, self.board, self.cfg, 2)
        self.slot1 = slots.slot_path(self.repo, self.cfg, 1)
        self.slot2 = slots.slot_path(self.repo, self.cfg, 2)
        for task_id in (7, 8):   # two task branches, each with its own commit
            git(self.repo_path, "branch", f"artel/{task_id}", INTEGRATION)
        self.sha = {t: git(self.repo_path, "rev-parse", f"artel/{t}") for t in (7, 8)}

    def take(self, idle=()):
        return repocheck.take(self.repo, self.slot1, idle_slots=list(idle))

    def compare(self, before, after, **kw):
        kw.setdefault("own_branch", "artel/7")
        kw.setdefault("may_commit", True)
        kw.setdefault("skip_refs", ())
        return repocheck.compare(before, after, repo=self.repo, **kw)

    def on_branch(self, n=7):
        slots.prepare_slot(self.repo, self.board, self.cfg, 1, n)

    def commit_in_slot(self, name="a.txt"):
        return self.commit_file(name, "x\n", f"add {name}", cwd=self.slot1)


class RefsTest(RepoCheckCase):
    def test_foreign_branch_move_detected_and_restored(self):
        """Mutation caught: a check that watches only the own branch (a foreign task's branch rewound)."""
        self.on_branch()
        other = self.commit_in_slot_detached()   # a commit to move artel/8 onto
        before = self.take()
        git(self.repo_path, "branch", "-f", "artel/8", other)
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["ref refs/heads/artel/8 moved"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/8"), self.sha[8])

    def test_deleted_foreign_branch_is_recreated(self):
        """Mutation caught: restore handling moves only (a deleted branch stays deleted)."""
        before = self.take()
        git(self.repo_path, "branch", "-D", "artel/8")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["ref refs/heads/artel/8 deleted"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/8"), self.sha[8])

    def test_stash_and_replace_ref_detected_and_removed(self):
        """Mutation caught: watching refs/heads/artel/ only (a stash or a replace ref survives the stage)."""
        before = self.take()
        (self.slot1 / "README").write_text("changed\n", encoding="utf-8", newline="\n")
        git(self.slot1, "stash", "push", "-q")
        git(self.repo_path, "replace", self.sha[7], git(self.repo_path, "rev-parse", "main"))
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(len(changes), 2, changes)
        self.assertTrue(any(c.startswith("ref refs/stash ") for c in changes), changes)
        self.assertTrue(any(c.startswith("ref refs/replace/") for c in changes), changes)
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "for-each-ref", "refs/stash", "refs/replace/"), "")

    def test_skip_refs_ignored(self):
        """Mutation caught: no skip list (the runner's own integration of another task is reported, and
        `restore` would revert it)."""
        moved = self.commit_in_slot_detached()
        before = self.take()
        git(self.repo_path, "branch", "-f", "artel/8", moved)   # a concurrent integrate of task 8
        git(self.repo_path, "branch", "-f", INTEGRATION, moved)
        after = self.take()
        self.assertEqual(self.compare(before, after), ["ref refs/heads/artel/8 moved"])
        self.assertEqual(self.compare(before, after, skip_refs={"refs/heads/artel/8"}), [])

    def commit_in_slot_detached(self):
        return self.commit_file("b.txt", "x\n", "detached work", cwd=self.slot2)

    def test_own_branch_may_gain_commits_only_for_writers(self):
        """Mutation caught: the same rule for every profile (a reviewer's commit passes), or no ancestry test
        (a writer that resets its branch backwards passes)."""
        self.on_branch()
        before = self.take()
        self.commit_in_slot()
        after = self.take()
        self.assertEqual(self.compare(before, after, may_commit=True), [])
        self.assertEqual(self.compare(before, after, may_commit=False), ["ref refs/heads/artel/7 moved"])
        git(self.slot1, "reset", "-q", "--hard", "HEAD~1")
        again = self.take()
        self.assertEqual(self.compare(after, again, may_commit=True), ["ref refs/heads/artel/7 moved"])
        git(self.slot1, "reset", "-q", "--hard", before.refs["refs/heads/artel/7"])

    def test_detached_head_move_detected_for_a_reader(self):
        """Mutation caught: no HEAD comparison (a reviewer committing on a detached slot leaves no ref change)."""
        before = self.take()
        self.commit_in_slot()
        after = self.take()
        self.assertEqual(self.compare(before, after, own_branch=None, may_commit=False), ["slot HEAD moved"])
        self.assertEqual(self.compare(before, after, own_branch=None, may_commit=True), [])
        self.assertEqual(repocheck.restore(self.repo, before, after, ["slot HEAD moved"]), [])


class FilesTest(RepoCheckCase):
    def test_hooks_path_config_and_info_attributes_restored(self):
        """Mutation caught: a refs-only check (core.hooksPath, a hook or info/attributes would run code of the
        agent's choosing in a later git command of the runner)."""
        before = self.take()
        config_before = (self.repo.common_dir / "config").read_bytes()
        git(self.repo_path, "config", "core.hooksPath", "elsewhere")
        (self.repo.common_dir / "info").mkdir(exist_ok=True)
        (self.repo.common_dir / "info" / "attributes").write_bytes(b"* filter=evil\n")
        (self.repo.common_dir / "hooks").mkdir(exist_ok=True)
        (self.repo.common_dir / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\nexit 1\n")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(sorted(changes), ["file config changed", "file hooks/pre-commit created",
                                           "file info/attributes created"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual((self.repo.common_dir / "config").read_bytes(), config_before)
        self.assertFalse((self.repo.common_dir / "info" / "attributes").exists())
        self.assertFalse((self.repo.common_dir / "hooks" / "pre-commit").exists())
        self.assertEqual(self.compare(before, self.take()), [])

    def test_deleted_info_exclude_is_rewritten(self):
        """Mutation caught: restore that only deletes created files (a deleted file is not written back)."""
        before = self.take()
        exclude = self.repo.common_dir / "info" / "exclude"
        data = exclude.read_bytes()
        exclude.unlink()
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["file info/exclude deleted"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(exclude.read_bytes(), data)

    def test_slot_worktree_config_is_watched(self):
        """Mutation caught: the slot's config.worktree missing from the watched files."""
        git(self.repo_path, "config", "extensions.worktreeConfig", "true")
        before = self.take()
        git(self.slot1, "config", "--worktree", "core.hooksPath", "x")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(len(changes), 1, changes)
        self.assertIn("config.worktree", changes[0])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(self.compare(before, self.take()), [])


class GitOwnWritesTest(RepoCheckCase):
    def test_info_refs_and_runner_branch_config_are_not_changes(self):
        """Mutation caught: `git gc` (info/refs) or a task branch created by the runner in another slot
        (`[branch "artel/N"]` in the shared config) failing every concurrent stage."""
        before = self.take()
        (self.repo.common_dir / "info").mkdir(exist_ok=True)
        (self.repo.common_dir / "info" / "refs").write_bytes(b"abc\trefs/heads/main\n")
        git(self.repo_path, "config", "branch.artel/9.remote", ".")
        git(self.repo_path, "config", "branch.artel/9.merge", "refs/heads/artel/integration")
        self.assertEqual(self.compare(before, self.take()), [])
        git(self.repo_path, "config", "core.hooksPath", "elsewhere")
        self.assertEqual(self.compare(before, self.take()), ["file config changed"])


class WorktreesTest(RepoCheckCase):
    def test_idle_slot_write_detected_and_cleaned(self):
        """Mutation caught: only the agent's own slot is watched (an idle slot dirtied from outside stays dirty
        and the next stage in it starts from that)."""
        before = self.take(idle=[(2, self.slot2)])
        (self.slot2 / "README").write_text("dirty\n", encoding="utf-8", newline="\n")
        (self.slot2 / "stray.txt").write_text("x\n", encoding="utf-8", newline="\n")
        after = self.take(idle=[(2, self.slot2)])
        changes = self.compare(before, after)
        self.assertEqual(changes, ["idle slot 2 changed"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.slot2, "status", "--porcelain"), "")

    def test_a_slot_that_got_a_job_is_not_compared(self):
        """Mutation caught: comparing slots present on one side only (the runner started a job in it)."""
        before = self.take(idle=[(2, self.slot2)])
        (self.slot2 / "README").write_text("the other job\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.compare(before, self.take(idle=[])), [])

    def test_restore_reports_unrestorable_worktree_change(self):
        """Mutation caught: restore claiming success for a changed worktree list (it only reports it)."""
        before = self.take()
        git(self.repo_path, "worktree", "add", "-q", "--detach", str(self.tmp / "extra"), "main")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["worktrees changed"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), ["worktrees changed"])
        self.assertEqual(self.compare(before, after, runner_changed_worktrees=True), [])


if __name__ == "__main__":
    unittest.main()

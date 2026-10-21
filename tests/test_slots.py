import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from artel import board as B, config as C, platform as P, slots
from artel.errors import ArtelError
from artel.gitutil import INTEGRATION, Repo
from tests.helpers import RepoTestCase, git, py_cmd


class SlotsCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)
        self.board = B.open_board(self.repo.board_path)
        self.addCleanup(self.board.close)
        self.cfg = self.make_cfg()

    def make_cfg(self, **cli):
        return C.load_config(self.repo, env={}, cli={k: (v, "test") for k, v in cli.items()})

    def slot(self, count=1, **cfg_overrides):
        if cfg_overrides:
            self.cfg = self.make_cfg(**cfg_overrides)
        return slots.ensure_slots(self.repo, self.board, self.cfg, count)

    def new_task(self):
        return self.board.insert_task(title="t", state="open", created_by="human")

    def head(self, path):
        return git(path, "rev-parse", "HEAD")

    def branch_of(self, path):
        return git(path, "branch", "--show-current")

    def commit_on_branch(self, n, task_id, name="work.txt"):
        path = slots.slot_path(self.repo, self.cfg, n)
        slots.prepare_slot(self.repo, self.board, self.cfg, n, task_id)
        return self.commit_file(name, "x\n", f"work {task_id}", cwd=path)


class EnsureTest(SlotsCase):
    def test_ensure_creates_detached_worktrees_under_slots_dir(self):
        self.assertEqual(self.slot(2), [1, 2])
        for n in (1, 2):
            path = self.repo_path / ".artel" / "slots" / f"artel-{n}"
            self.assertTrue(path.is_dir())
            self.assertEqual(self.branch_of(path), "")   # detached
            self.assertEqual(self.head(path), git(self.repo_path, "rev-parse", INTEGRATION))
        self.assertEqual([(s.n, Path(s.path)) for s in self.board.list_slots()],
                         [(n, slots.slot_path(self.repo, self.cfg, n)) for n in (1, 2)])
        self.assertEqual(slots.slot_path(self.repo, self.cfg, 1), self.repo.root / ".artel/slots/artel-1")

    def test_ensure_slots_excludes_slots_dir(self):
        """Mutation caught: no exclude line (slots show up as untracked) or one appended on every call."""
        self.assertEqual(git(self.repo_path, "status", "--porcelain"), "")
        self.slot(2)
        self.slot(2)
        self.assertEqual(git(self.repo_path, "status", "--porcelain"), "")
        exclude = (self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(exclude.count("/.artel/slots/"), 1)

    def test_ensure_slots_excludes_custom_slots_dir_once(self):
        """Mutation caught: the exclude line hard-coding the default directory."""
        self.slot(1, slots_dir="work/pool")
        self.slot(1, slots_dir="work/pool")
        exclude = (self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(exclude.count("/work/pool/"), 1)
        self.assertNotIn("/.artel/slots/", exclude)

    def test_exclude_appends_after_unterminated_crlf_file(self):
        """Mutation caught: no newline before the line, CRLF dedupe dropped, missing info/ dir not created."""
        exclude = self.repo.common_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(b"foo/\r\n/.artel/slots/\r\nbar")
        self.slot(1)
        self.assertEqual(exclude.read_bytes(), b"foo/\r\n/.artel/slots/\r\nbar")   # CRLF line counts as present
        exclude.write_bytes(b"bar")
        self.slot(1)
        self.assertEqual(exclude.read_bytes(), b"bar\n/.artel/slots/\n")

    def test_exclude_skipped_outside_repo_and_creates_info_dir(self):
        """Mutation caught: a crash or a '/../x/' line for a slots_dir outside the repository."""
        import shutil
        shutil.rmtree(self.repo.common_dir / "info", ignore_errors=True)
        self.slot(1)
        self.assertIn("/.artel/slots/", (self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertIsNone(slots._exclude_slots_dir(self.repo, {"slots_dir": "../outside-pool"}))
        self.assertNotIn("outside-pool", (self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8"))

    def test_scrub_after_unmerged_entries_keeps_slot_usable(self):
        """Mutation caught: the flag clean-up treating an unmerged entry (`ls-files -v` tag `M`) as a hidden file
        and raising (the slot is dead for every later job)."""
        [n] = self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, n)
        self.commit_on_branch(n, 5, name="clash.txt")
        git(path, "switch", "--detach", INTEGRATION)
        other = self.commit_file("clash.txt", "other\n", "other side", cwd=path)
        git(path, "cherry-pick", "artel/5", check=False)   # conflicts: unmerged entries in the index
        git(path, "cherry-pick", "--quit")
        self.assertIn("M clash.txt", git(path, "ls-files", "-v"))
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 6)
        self.assertEqual(git(path, "status", "--porcelain"), "")

    def test_slots_dir_with_dotdot_is_normalized(self):
        """Mutation caught: the unnormalized `relative_to` (`x/../.artel/slots` writes a '/x/../.artel/slots/' line
        that hides nothing; `../outside` must still write none)."""
        (self.repo_path / "x").mkdir()
        self.assertEqual(git(self.repo_path, "status", "--porcelain"), "")
        slots._exclude_slots_dir(self.repo, {"slots_dir": "x/../.artel/slots"})
        exclude = (self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(exclude.count("/.artel/slots/"), 1)
        self.assertFalse([l for l in exclude if ".." in l])
        before = list(exclude)
        slots._exclude_slots_dir(self.repo, {"slots_dir": "a/../../outside"})
        self.assertEqual((self.repo.common_dir / "info" / "exclude").read_text(encoding="utf-8").splitlines(), before)

    def test_ensure_is_idempotent(self):
        self.slot(2)
        marker = slots.slot_path(self.repo, self.cfg, 1) / "keep.txt"
        marker.write_text("x", encoding="utf-8")
        self.assertEqual(self.slot(3), [1, 2, 3])
        self.assertTrue(marker.exists())   # an existing slot is neither recreated nor scrubbed
        self.assertEqual(len(self.board.list_slots()), 3)

    def test_ensure_recreates_a_vanished_slot(self):
        self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, 1)
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        self.slot(1)
        self.assertTrue(path.is_dir())

    def test_ensure_refuses_a_slots_dir_inside_git(self):
        with self.assertRaises(ArtelError):
            self.slot(1, slots_dir=".git/slots")

    def test_ensure_refuses_a_foreign_directory(self):
        path = slots.slot_path(self.repo, self.cfg, 1)
        path.mkdir(parents=True)
        (path / "precious.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(ArtelError):
            self.slot(1)
        self.assertEqual((path / "precious.txt").read_text(encoding="utf-8"), "mine")


class AdoptionTest(SlotsCase):
    """`.claude/worktrees` is also where Claude Code and users keep their own worktrees (data-loss review)."""

    def user_worktree(self, n=1, *opts, commit=None):
        path = slots.slot_path(self.repo, self.cfg, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo_path, "worktree", "add", "-q", *opts, str(path), *([commit] if commit else []))
        return path

    def test_ensure_refuses_a_users_worktree_on_a_feature_branch(self):
        """Mutation caught: adopting any existing worktree (the branch rule)."""
        path = self.user_worktree(1, "-b", "userfeature")
        (path / "wip.txt").write_text("precious", encoding="utf-8")
        with self.assertRaises(ArtelError) as cm:
            self.slot(1)
        self.assertIn("userfeature", str(cm.exception))
        self.assertEqual(self.board.list_slots(), [])
        self.assertEqual((path / "wip.txt").read_text(encoding="utf-8"), "precious")
        self.assertEqual(self.branch_of(path), "userfeature")

    def test_ensure_refuses_a_dirty_detached_worktree_it_did_not_create(self):
        """Mutation caught: dropping the clean-status rule (the branch rule alone passes a detached worktree)."""
        path = self.user_worktree(1, "--detach", commit="main")
        (path / "wip.txt").write_text("precious", encoding="utf-8")
        with self.assertRaises(ArtelError) as cm:
            self.slot(1)
        self.assertIn("uncommitted", str(cm.exception))
        (path / "README").write_text("edited", encoding="utf-8")
        with self.assertRaises(ArtelError):
            self.slot(1)
        self.assertEqual((path / "wip.txt").read_text(encoding="utf-8"), "precious")

    def test_ensure_adopts_a_clean_detached_or_artel_branch_worktree(self):
        path = self.user_worktree(1, "--detach", commit="main")
        self.assertEqual(self.slot(1), [1])
        self.assertEqual([s.n for s in self.board.list_slots()], [1])
        path2 = self.user_worktree(2, "-b", "artel/7")
        self.assertEqual(self.slot(2), [1, 2])
        self.assertTrue(path.is_dir() and path2.is_dir())

    def test_clean_check_ignores_slot_keep_paths(self):
        path = self.user_worktree(1, "--detach", commit="main")
        (path / "deps_cache").mkdir()
        (path / "deps_cache" / "dep.js").write_text("x", encoding="utf-8")
        with self.assertRaises(ArtelError):
            self.slot(1)
        self.assertEqual(self.slot(1, slot_keep=["deps_cache"]), [1])

    def test_a_registered_slot_with_leftover_dirt_is_kept(self):
        """Mutation caught: refusing artel's own slot after a killed agent left uncommitted files."""
        self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, 1)
        (path / "half.txt").write_text("unfinished", encoding="utf-8")
        self.assertEqual(self.slot(1), [1])
        self.assertTrue((path / "half.txt").exists())

    def test_ensure_refuses_a_link_in_place_of_a_slot(self):
        """Mutation caught: following a symlink/junction slot path into somebody's (clean) worktree."""
        user = self.repo_path / ".claude" / "worktrees" / "mywork"   # clean and detached: nothing else refuses it
        user.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo_path, "worktree", "add", "-q", "--detach", str(user), "main")
        link = slots.slot_path(self.repo, self.cfg, 2)
        link.parent.mkdir(parents=True, exist_ok=True)
        P.link_dir(link, user)
        with self.assertRaises(ArtelError) as cm:
            self.slot(2)
        self.assertIn("link", str(cm.exception))
        self.assertNotIn(2, [s.n for s in self.board.list_slots()])   # the link was never registered

    def test_guard_refuses_a_slot_replaced_by_a_link(self):
        """Mutation caught: the guard comparing resolved paths only."""
        import shutil
        self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, 1)
        user = self.repo_path / ".claude" / "worktrees" / "mywork"   # inside slots_dir: the old guard passed this
        user.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo_path, "worktree", "add", "-q", "--detach", str(user), "main")
        (user / "wip.txt").write_text("precious", encoding="utf-8")
        shutil.rmtree(path)
        git(self.repo_path, "worktree", "prune")
        P.link_dir(path, user)
        with self.assertRaises(slots.SlotGuardError) as cm:
            slots.leave_slot(self.repo, self.board, self.cfg, 1)
        self.assertIn("link", str(cm.exception))
        self.assertEqual((user / "wip.txt").read_text(encoding="utf-8"), "precious")

    def test_ensure_refuses_a_regular_file_at_the_slot_path(self):
        path = slots.slot_path(self.repo, self.cfg, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ArtelError):
            self.slot(1)


class PrepareTest(SlotsCase):
    def test_prepare_new_branch_from_integration_tip(self):
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        tip = self.commit_file("int.txt", "i\n", "on integration")
        git(self.repo_path, "checkout", "-q", "main")
        [n] = self.slot()
        sha = slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
        self.assertEqual(sha, tip)
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertEqual(self.branch_of(path), "artel/5")
        self.assertTrue((path / "int.txt").exists())

    def test_prepare_resumes_existing_branch(self):
        [n] = self.slot()
        work = self.commit_on_branch(n, 5)
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertEqual(self.branch_of(slots.slot_path(self.repo, self.cfg, n)), "")
        self.assertEqual(slots.prepare_slot(self.repo, self.board, self.cfg, n, 5), work)   # resume after ASK

    def test_prepare_after_reset_branch_starts_fresh(self):
        [n] = self.slot()
        tid = self.new_task()
        self.commit_on_branch(n, tid)
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        slots.apply_git_effects(self.repo, self.board, self.cfg, tid, ["reset_branch"])
        self.assertFalse(slots.task_branch_exists(self.repo, tid))
        self.assertEqual(slots.prepare_slot(self.repo, self.board, self.cfg, n, tid),
                         git(self.repo_path, "rev-parse", INTEGRATION))   # reset after FAIL

    def test_clean_keeps_slot_keep_paths(self):
        [n] = self.slot()
        path = slots.slot_path(self.repo, self.cfg, n)
        (path / "node_modules").mkdir()
        (path / "node_modules" / "x").write_text("dep", encoding="utf-8")
        (path / "junk.txt").write_text("junk", encoding="utf-8")
        (path / "build").mkdir()
        (path / "build" / "out.o").write_text("o", encoding="utf-8")
        self.commit_file(".gitignore", "build/\n", "ignore build", cwd=self.repo_path)
        git(self.repo_path, "branch", "-f", INTEGRATION, "main")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 1)
        self.assertTrue((path / "node_modules" / "x").exists())
        self.assertFalse((path / "junk.txt").exists())
        self.assertFalse((path / "build").exists())   # ignored files are removed too (-x)

    def test_clean_keep_list_is_configurable(self):
        [n] = self.slot(1, slot_keep=[".venv"])
        path = slots.slot_path(self.repo, self.cfg, n)
        for name in (".venv", "node_modules"):
            (path / name).mkdir()
            (path / name / "f").write_text("x", encoding="utf-8")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 1)
        self.assertTrue((path / ".venv" / "f").exists())
        self.assertFalse((path / "node_modules").exists())

    def test_uncommitted_changes_discarded(self):
        [n] = self.slot()
        path = slots.slot_path(self.repo, self.cfg, n)
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 1)
        (path / "README").write_text("dirty\n", encoding="utf-8")
        (path / "new.txt").write_text("n", encoding="utf-8")
        git(path, "add", "new.txt")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 2)
        self.assertEqual((path / "README").read_text(encoding="utf-8"), "seed\n")
        self.assertFalse((path / "new.txt").exists())
        self.assertEqual(git(path, "status", "--porcelain"), "")

    def test_branch_held_by_other_worktree_is_refused(self):
        git(self.repo_path, "branch", "artel/5", INTEGRATION)
        other = self.tmp / "human"
        git(self.repo_path, "worktree", "add", str(other), "artel/5")
        [n] = self.slot()
        path = slots.slot_path(self.repo, self.cfg, n)
        (path / "junk.txt").write_text("junk", encoding="utf-8")
        with self.assertRaises(slots.SlotBusy) as cm:
            slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
        self.assertEqual(cm.exception.where.resolve(), other.resolve())
        self.assertTrue((path / "junk.txt").exists())   # refused before anything was scrubbed
        self.assertIn("artel/5", str(cm.exception))
        self.assertIsNotNone(slots.checked_out_elsewhere(self.repo, "artel/5"))
        self.assertIsNone(slots.checked_out_elsewhere(self.repo, "artel/5", except_path=other))
        self.assertIsNone(slots.checked_out_elsewhere(self.repo, "artel/404"))

    def test_slot_reused_across_tasks(self):
        [n] = self.slot()
        path = slots.slot_path(self.repo, self.cfg, n)
        for tid in (1, 2):
            slots.prepare_slot(self.repo, self.board, self.cfg, n, tid)
            self.assertEqual(self.branch_of(path), f"artel/{tid}")
            slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertTrue(slots.task_branch_exists(self.repo, 1))
        self.assertTrue(slots.task_branch_exists(self.repo, 2))

    def test_detach_keeps_branch(self):
        [n] = self.slot()
        work = self.commit_on_branch(n, 5)
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertEqual(self.branch_of(path), "")
        self.assertEqual(self.head(path), work)   # HEAD commit kept
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/5"), work)

    def test_leave_aborts_merge_in_progress(self):
        [n] = self.slot()
        self.make_conflict(n)
        path = slots.slot_path(self.repo, self.cfg, n)
        slots.start_conflict_merge(self.repo, self.board, self.cfg, n)
        self.assertTrue(self.merge_in_progress(path))
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertFalse(self.merge_in_progress(path))
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertEqual(self.branch_of(path), "")

    def merge_in_progress(self, path):
        return subprocess_ok(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")

    def make_conflict(self, n):
        self.commit_on_branch(n, 5, name="shared.txt")
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "different\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)

    def test_start_conflict_merge_reports_paths(self):
        [n] = self.slot()
        self.make_conflict(n)
        self.assertEqual(slots.start_conflict_merge(self.repo, self.board, self.cfg, n), ["shared.txt"])
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertTrue(self.merge_in_progress(path))   # stays in progress for the agent
        self.assertIn("<<<<<<<", (path / "shared.txt").read_text(encoding="utf-8"))

    def test_start_conflict_merge_without_conflicts_stays_in_progress(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5, name="mine.txt")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("theirs.txt", "t\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
        self.assertEqual(slots.start_conflict_merge(self.repo, self.board, self.cfg, n), [])
        self.assertTrue(self.merge_in_progress(slots.slot_path(self.repo, self.cfg, n)))

    def test_prepare_detached_at_integration_tip(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5)
        path = slots.slot_path(self.repo, self.cfg, n)
        (path / "junk.txt").write_text("j", encoding="utf-8")
        sha = slots.prepare_detached(self.repo, self.board, self.cfg, n)
        self.assertEqual(sha, git(self.repo_path, "rev-parse", INTEGRATION))
        self.assertEqual(self.branch_of(path), "")
        self.assertFalse((path / "junk.txt").exists())

    def test_merge_identity_args_fallback(self):
        pair = ["-c", "user.name=artel", "-c", "user.email=artel@localhost"]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(slots.merge_identity_args(self.repo), pair)
            git(self.repo_path, "config", "user.email", "me@example.invalid")
            self.assertEqual(slots.merge_identity_args(self.repo), pair)   # a name is missing too
            git(self.repo_path, "config", "user.name", "Me")
            self.assertEqual(slots.merge_identity_args(self.repo), [])
        git(self.repo_path, "config", "--unset", "user.email")
        git(self.repo_path, "config", "--unset", "user.name")
        self.assertEqual(slots.merge_identity_args(self.repo), [])   # identity from the environment (tests/__init__)

    def test_conflict_merge_works_without_any_identity(self):
        [n] = self.slot()
        self.make_conflict(n)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(slots.start_conflict_merge(self.repo, self.board, self.cfg, n), ["shared.txt"])


def subprocess_ok(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True).returncode == 0


class GuardTest(SlotsCase):
    def test_guard_accepts_a_registered_slot(self):
        [n] = self.slot()
        self.assertEqual(slots.guard(self.repo, self.board, self.cfg, n), slots.slot_path(self.repo, self.cfg, n))

    def test_guard_refuses_unregistered_path(self):
        self.slot(1)
        hand = slots.slot_path(self.repo, self.cfg, 2)
        git(self.repo_path, "worktree", "add", "--detach", str(hand), INTEGRATION)   # not registered in the board
        (hand / "mine.txt").write_text("precious", encoding="utf-8")
        for call in (lambda: slots.guard(self.repo, self.board, self.cfg, 2),
                     lambda: slots.prepare_slot(self.repo, self.board, self.cfg, 2, 1),
                     lambda: slots.prepare_detached(self.repo, self.board, self.cfg, 2),
                     lambda: slots.leave_slot(self.repo, self.board, self.cfg, 2),
                     lambda: slots.start_conflict_merge(self.repo, self.board, self.cfg, 2)):
            with self.assertRaises(slots.SlotGuardError):
                call()
        self.assertEqual((hand / "mine.txt").read_text(encoding="utf-8"), "precious")

    def test_guard_refuses_registered_path_outside_slots_dir(self):
        self.slot(1)
        (self.repo_path / "untracked.txt").write_text("keep", encoding="utf-8")
        (self.repo_path / "README").write_text("edited\n", encoding="utf-8")
        self.board.upsert_slot(1, str(self.repo_path))   # a corrupt board row pointing at the main worktree
        for call in (lambda: slots.leave_slot(self.repo, self.board, self.cfg, 1),
                     lambda: slots.prepare_slot(self.repo, self.board, self.cfg, 1, 1),
                     lambda: slots.reconcile_slots(self.repo, self.board, self.cfg)):
            with self.assertRaises(slots.SlotGuardError):
                call()
        self.assertEqual((self.repo_path / "untracked.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.repo_path / "README").read_text(encoding="utf-8"), "edited\n")
        self.assertEqual(self.branch_of(self.repo_path), "main")

    def test_guard_refuses_a_row_pointing_at_another_slots_directory(self):
        self.slot(2)
        self.board.upsert_slot(1, str(slots.slot_path(self.repo, self.cfg, 2)))
        with self.assertRaises(slots.SlotGuardError):
            slots.guard(self.repo, self.board, self.cfg, 1)

    def test_guard_refuses_a_plain_directory_and_a_missing_one(self):
        self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, 1)
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        with self.assertRaises(slots.SlotGuardError):
            slots.guard(self.repo, self.board, self.cfg, 1)
        path.mkdir()   # exists but is not a worktree: git would resolve the toplevel to the main repository
        with self.assertRaises(slots.SlotGuardError):
            slots.guard(self.repo, self.board, self.cfg, 1)


class HardeningTest(SlotsCase):
    def test_ensure_refuses_to_adopt_the_main_worktree(self):
        root = self.tmp / "artel-1"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "commit.gpgsign", "false")
        (root / "f").write_text("x", encoding="utf-8")
        git(root, "add", "f")
        git(root, "commit", "-q", "-m", "seed")
        git(root, "branch", INTEGRATION, "main")
        repo = Repo.discover(root)
        board = B.open_board(self.tmp / "other.db")
        self.addCleanup(board.close)
        cfg = C.load_config(repo, env={}, cli={"slots_dir": ("..", "test")})   # slot 1 would be the repo itself
        (root / "untracked.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ArtelError):
            slots.ensure_slots(repo, board, cfg, 1)
        self.assertEqual(board.list_slots(), [])
        self.assertEqual((root / "untracked.txt").read_text(encoding="utf-8"), "keep")

    def test_guard_refuses_another_repository_in_the_slot_directory(self):
        import shutil
        self.slot(1)
        path = slots.slot_path(self.repo, self.cfg, 1)
        shutil.rmtree(path)
        path.mkdir()
        git(path, "init", "-q", "-b", "main")   # an unrelated repository where the slot should be
        (path / "theirs.txt").write_text("keep", encoding="utf-8")
        for call in (lambda: slots.guard(self.repo, self.board, self.cfg, 1),
                     lambda: slots.leave_slot(self.repo, self.board, self.cfg, 1),
                     lambda: slots.reconcile_slots(self.repo, self.board, self.cfg)):
            with self.assertRaises(slots.SlotGuardError):
                call()
        self.assertEqual((path / "theirs.txt").read_text(encoding="utf-8"), "keep")

    def test_guard_refuses_a_copied_slot_directory(self):
        # catches: checking only that the git dir lies in this repository, not that it belongs to this directory
        import shutil
        self.slot(2)
        one, two = (slots.slot_path(self.repo, self.cfg, n) for n in (1, 2))
        shutil.rmtree(two)
        shutil.copytree(one, two)   # its .git file still names slot 1's administration directory
        with self.assertRaises(slots.SlotGuardError):
            slots.guard(self.repo, self.board, self.cfg, 2)
        slots.prepare_slot(self.repo, self.board, self.cfg, 1, 5)
        with self.assertRaises(slots.SlotGuardError):
            slots.prepare_slot(self.repo, self.board, self.cfg, 2, 7)
        self.assertEqual(self.branch_of(one), "artel/5")   # slot 1 was not moved to another branch

    def test_guard_refuses_a_git_file_pointing_at_a_users_worktree(self):
        self.slot(1)
        user = self.tmp / "userwork"
        git(self.repo_path, "worktree", "add", "-q", "-b", "user", str(user))
        (user / "wip.txt").write_text("precious", encoding="utf-8")
        admin = git(user, "rev-parse", "--path-format=absolute", "--git-dir")
        slot = slots.slot_path(self.repo, self.cfg, 1)
        (slot / ".git").unlink()   # Git for Windows marks it hidden: opening it with 'w' is denied, so replace it
        (slot / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8", newline="\n")
        with self.assertRaises(slots.SlotGuardError):
            slots.leave_slot(self.repo, self.board, self.cfg, 1)
        self.assertEqual((user / "wip.txt").read_text(encoding="utf-8"), "precious")
        self.assertEqual(self.branch_of(user), "user")

    def test_leave_removes_a_stale_index_lock(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5)
        path = slots.slot_path(self.repo, self.cfg, n)
        lock = Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
        lock.write_text("", encoding="utf-8")   # left by a writer killed during `git add`
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertFalse(lock.exists())
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)
        self.assertFalse(slots.task_branch_exists(self.repo, 5))

    def test_inherited_git_dir_does_not_redirect_slot_commands(self):
        [n] = self.slot()
        env = {"GIT_DIR": str(self.repo.common_dir), "GIT_WORK_TREE": str(self.repo_path)}
        with mock.patch.dict(os.environ, env):
            slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
            slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertEqual(self.branch_of(self.repo_path), "main")   # the main worktree was never touched
        self.assertTrue(slots.task_branch_exists(self.repo, 5))

    def test_leave_clears_a_rebase_left_by_a_killed_writer(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5, name="shared.txt")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("shared.txt", "other\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        path = slots.slot_path(self.repo, self.cfg, n)
        rebase = subprocess.run(["git", "rebase", INTEGRATION], cwd=path, capture_output=True)
        self.assertNotEqual(rebase.returncode, 0)   # conflict: rebase-merge state is left behind
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        self.assertEqual(self.branch_of(path), "")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)   # the branch is free again
        self.assertFalse(slots.task_branch_exists(self.repo, 5))

    def test_leave_clears_a_bisect_session(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5)
        path = slots.slot_path(self.repo, self.cfg, n)
        git(path, "bisect", "start")
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)
        self.assertFalse(slots.task_branch_exists(self.repo, 5))

    def test_recreated_slot_forgets_its_setup_hash(self):
        import shutil
        counter = self.tmp / "c.txt"
        code = f"open({json.dumps(str(counter))}, 'a', encoding='utf-8').write('x')"
        [n] = self.slot(1, **{"commands.setup": py_cmd(code)})
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertEqual(slots.run_setup_if_needed(self.repo, self.board, self.cfg, n, path, task_id=None), "ok")
        shutil.rmtree(path)
        slots.reconcile_slots(self.repo, self.board, self.cfg)
        self.slot(1)
        self.assertIsNone(self.board.list_slots()[0].setup_hash)
        self.assertEqual(slots.run_setup_if_needed(self.repo, self.board, self.cfg, n, path, task_id=None), "ok")
        self.assertEqual(counter.read_text(encoding="utf-8"), "xx")

    def test_conflict_paths_are_not_c_quoted(self):
        [n] = self.slot()
        name = "ключ файл.txt"
        self.commit_on_branch(n, 5, name=name)
        slots.leave_slot(self.repo, self.board, self.cfg, n)
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file(name, "different\n", "integration edit")
        git(self.repo_path, "checkout", "-q", "main")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
        self.assertEqual(slots.start_conflict_merge(self.repo, self.board, self.cfg, n), [name])

    def test_slots_dir_inside_git_or_at_the_root_is_refused(self):
        for bad in (".git", ".git/slots", "x/.git/y", ".", ".."):
            with self.assertRaises(ArtelError, msg=bad):
                self.slot(1, slots_dir=bad)

    def test_delete_task_branch_survives_a_vanished_slot(self):
        import shutil
        [n] = self.slot()
        self.commit_on_branch(n, 5)
        shutil.rmtree(slots.slot_path(self.repo, self.cfg, n))   # the slot vanished while holding the branch
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)
        self.assertFalse(slots.task_branch_exists(self.repo, 5))

    def test_prepare_ignores_a_vanished_holder_of_the_branch(self):
        import shutil
        self.slot(2)
        self.commit_on_branch(1, 5)
        shutil.rmtree(slots.slot_path(self.repo, self.cfg, 1))
        slots.prepare_slot(self.repo, self.board, self.cfg, 2, 5)   # no SlotBusy for a pruned worktree
        self.assertEqual(self.branch_of(slots.slot_path(self.repo, self.cfg, 2)), "artel/5")

    def test_reconcile_keeps_detaching_after_a_git_error(self):
        self.slot(2)
        self.commit_on_branch(2, 5)
        real = slots.leave_slot

        def flaky(repo, board, cfg, n):
            if n == 1:
                raise slots.GitError(["switch"], "locked file")
            return real(repo, board, cfg, n)

        with mock.patch.object(slots, "leave_slot", flaky), self.assertRaises(slots.GitError):
            slots.reconcile_slots(self.repo, self.board, self.cfg)
        self.assertEqual(self.branch_of(slots.slot_path(self.repo, self.cfg, 2)), "")

    def test_refused_merge_is_an_error_not_an_empty_conflict_list(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5, name="mine.txt")
        git(self.repo_path, "checkout", "-q", INTEGRATION)
        self.commit_file("incoming.txt", "i\n", "integration adds a file")
        git(self.repo_path, "checkout", "-q", "main")
        slots.prepare_slot(self.repo, self.board, self.cfg, n, 5)
        (slots.slot_path(self.repo, self.cfg, n) / "incoming.txt").write_text("untracked", encoding="utf-8")
        with self.assertRaises(slots.GitError):   # git refuses (exit 2): untracked file in the way
            slots.start_conflict_merge(self.repo, self.board, self.cfg, n)

    def test_setup_run_row_carries_round_and_killed_flags(self):
        tid = self.new_task()
        task = self.board.get_task(tid)
        self.board.update_task(tid, expect_state="open", expect_version=task.version, round=3)
        [n] = self.slot(1, **{"commands.setup": py_cmd("import time; time.sleep(30)"), "setup_timeout": "1s",
                              "kill_grace": "1s"})
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertEqual(slots.run_setup_if_needed(self.repo, self.board, self.cfg, n, path, task_id=tid), "timeout")
        (run,) = self.board.list_runs(tid)
        self.assertEqual((run.round, run.timed_out, run.killed, run.outcome), (3, True, True, "timeout"))


class BranchTest(SlotsCase):
    def test_delete_task_branch_detaches_slot_first(self):
        [n] = self.slot()
        self.commit_on_branch(n, 5)
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)
        self.assertFalse(slots.task_branch_exists(self.repo, 5))
        path = slots.slot_path(self.repo, self.cfg, n)
        self.assertEqual(self.branch_of(path), "")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        slots.delete_task_branch(self.repo, self.board, self.cfg, 5)   # absent: a no-op

    def test_delete_task_branch_refuses_a_foreign_worktree(self):
        git(self.repo_path, "branch", "artel/7", INTEGRATION)
        other = self.tmp / "human"
        git(self.repo_path, "worktree", "add", str(other), "artel/7")
        self.slot()
        with self.assertRaises(slots.SlotBusy):
            slots.delete_task_branch(self.repo, self.board, self.cfg, 7)
        self.assertTrue(slots.task_branch_exists(self.repo, 7))

    def test_apply_git_effects_ignores_other_effects(self):
        git(self.repo_path, "branch", "artel/8", INTEGRATION)
        self.slot()
        slots.apply_git_effects(self.repo, self.board, self.cfg, 8, ["notify", "terminate_running"])
        self.assertTrue(slots.task_branch_exists(self.repo, 8))
        slots.apply_git_effects(self.repo, self.board, self.cfg, 8, ["notify", "delete_branch"])
        self.assertFalse(slots.task_branch_exists(self.repo, 8))

    def test_reconcile_prunes_and_detaches(self):
        self.slot(2)
        tid = 5
        self.commit_on_branch(2, tid)
        gone = slots.slot_path(self.repo, self.cfg, 1)
        import shutil
        shutil.rmtree(gone)
        slots.reconcile_slots(self.repo, self.board, self.cfg)
        listed = [wt.path.resolve() for wt in self.repo.worktrees()]
        self.assertNotIn(gone.resolve(), listed)   # pruned
        two = slots.slot_path(self.repo, self.cfg, 2)
        self.assertEqual(self.branch_of(two), "")   # detached
        self.assertTrue(slots.task_branch_exists(self.repo, tid))
        self.slot(2)   # the vanished slot is re-created lazily
        self.assertTrue(gone.is_dir())


class SetupTest(SlotsCase):
    def counter_cmd(self, extra=""):
        self.counter = self.tmp / "counter.txt"
        return py_cmd(f"open({json.dumps(str(self.counter))}, 'a', encoding='utf-8').write('x')\n{extra}")

    def runs(self):
        return self.counter.read_text(encoding="utf-8") if self.counter.exists() else ""

    def run_setup(self, n=1, task_id=None, **kw):
        path = slots.slot_path(self.repo, self.cfg, n)
        return slots.run_setup_if_needed(self.repo, self.board, self.cfg, n, path, task_id=task_id, **kw)

    def leave(self, n=1):
        slots.leave_slot(self.repo, self.board, self.cfg, n)

    def test_leave_slot_clears_setup_hash_when_setup_output_is_removed(self):
        """Mutation caught: a slot that runs without its environment (the setup output is not in slot_keep, the
        scrub deletes it, the stored hash says setup is done — PD-73)."""
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(
            "import os; os.makedirs('.cache-env', exist_ok=True); open('.cache-env/x', 'w', encoding='utf-8').write('1')")})
        self.assertEqual(self.run_setup(), "ok")
        self.assertEqual(json.loads(self.board.get_meta(f"setup_outputs:{n}")), [".cache-env"])
        self.leave()
        self.assertIsNone(self.board.list_slots()[0].setup_hash)
        self.assertIsNone(self.board.get_meta(f"setup_outputs:{n}"))
        self.assertFalse((slots.slot_path(self.repo, self.cfg, n) / ".cache-env").exists())
        self.assertEqual(self.run_setup(), "ok")
        self.assertEqual(self.runs(), "xx")   # setup ran again for the next stage

    def test_kept_setup_output_keeps_hash(self):
        """Mutation caught: the hash cleared after every stage (a setup run per stage for the `.venv` layout)."""
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(
            "import os; os.makedirs('.venv', exist_ok=True); open('.venv/x', 'w', encoding='utf-8').write('1')")})
        self.assertEqual(self.run_setup(), "ok")
        self.leave()
        self.assertIsNotNone(self.board.list_slots()[0].setup_hash)
        self.assertTrue((slots.slot_path(self.repo, self.cfg, n) / ".venv" / "x").exists())
        self.assertEqual(self.run_setup(), "skipped")
        self.assertEqual(self.runs(), "x")

    def test_gitignored_setup_output_invalidates_the_hash(self):
        """Mutation caught: `!!` entries dropped from the setup-output record (a gitignored `build/` is the common
        case, and the scrub deletes it)."""
        exclude = self.repo.common_dir / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("build/\n")
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(
            "import os; os.makedirs('build', exist_ok=True); open('build/x', 'w', encoding='utf-8').write('1')")})
        self.assertEqual(self.run_setup(), "ok")
        self.assertEqual(json.loads(self.board.get_meta(f"setup_outputs:{n}")), ["build"])
        self.leave()
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_untracked_files_all_does_not_hide_setup_output(self):
        """Mutation caught: `status.showUntrackedFiles=no` hiding the setup output from the record."""
        git(self.repo_path, "config", "status.showUntrackedFiles", "no")
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(
            "import os; os.makedirs('out', exist_ok=True); open('out/x', 'w', encoding='utf-8').write('1')")})
        self.assertEqual(self.run_setup(), "ok")
        self.assertEqual(json.loads(self.board.get_meta(f"setup_outputs:{n}")), ["out"])

    def test_leave_slot_clears_the_hash_before_the_scrub(self):
        """Mutation caught: the hash cleared after the scrub (a crash in between leaves no environment and a valid
        hash)."""
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(
            "import os; os.makedirs('.cache-env', exist_ok=True); open('.cache-env/x', 'w', encoding='utf-8').write('1')")})
        self.assertEqual(self.run_setup(), "ok")
        with mock.patch.object(slots, "scrub", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.leave()
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_that_leaves_nothing_untracked_keeps_hash(self):
        """Mutation caught: a tracked file rewritten by setup counted as output (reset restores it anyway)."""
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd("open('README', 'w', encoding='utf-8').write('changed')")})
        self.assertEqual(self.run_setup(), "ok")
        self.assertEqual(json.loads(self.board.get_meta(f"setup_outputs:{n}")), [])
        self.leave()
        self.assertIsNotNone(self.board.list_slots()[0].setup_hash)

    def test_setup_hash_changes_with_inputs_and_command(self):
        d = self.tmp / "h"
        d.mkdir()
        (d / "lock.txt").write_text("a", encoding="utf-8")
        base = slots.setup_hash(d, ["npm", "ci"], ["lock.txt"])
        self.assertEqual(base, slots.setup_hash(d, ["npm", "ci"], ["lock.txt"]))
        self.assertNotEqual(base, slots.setup_hash(d, ["npm", "install"], ["lock.txt"]))
        self.assertNotEqual(base, slots.setup_hash(d, "npm ci", ["lock.txt"]))
        (d / "lock.txt").write_text("b", encoding="utf-8")
        self.assertNotEqual(base, slots.setup_hash(d, ["npm", "ci"], ["lock.txt"]))
        self.assertNotEqual(slots.setup_hash(d, ["x"], ["missing.txt"]), slots.setup_hash(d, ["x"], []))
        self.assertNotEqual(slots.setup_hash(d, ["x"], ["a", "b"]), slots.setup_hash(d, ["x"], ["b", "a"]))   # order
        self.assertEqual(slots.setup_hash(d, ["x"], ["sub\\f"]), slots.setup_hash(d, ["x"], ["sub/f"]))   # one separator

    def test_setup_hash_is_independent_of_line_endings_policy_of_the_reader(self):
        d = self.tmp / "h"
        d.mkdir()
        (d / "f").write_bytes(b"a\r\nb\n")
        first = slots.setup_hash(d, ["x"], ["f"])
        (d / "f").write_bytes(b"a\nb\n")
        self.assertNotEqual(first, slots.setup_hash(d, ["x"], ["f"]))   # bytes, not text: no newline folding

    def test_setup_skipped_when_hash_unchanged(self):
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd()})
        self.assertEqual(self.run_setup(n), "ok")
        self.assertEqual(self.run_setup(n), "skipped")
        self.assertEqual(self.runs(), "x")   # ran once for two prepares
        self.assertIsNotNone(self.board.list_slots()[0].setup_hash)

    def test_setup_reruns_when_an_input_changes(self):
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(), "commands.setup_inputs": ["lock.txt"]})
        path = slots.slot_path(self.repo, self.cfg, n)
        (path / "lock.txt").write_text("v1", encoding="utf-8")
        self.assertEqual(self.run_setup(n), "ok")
        (path / "lock.txt").write_text("v2", encoding="utf-8")
        self.assertEqual(self.run_setup(n), "ok")
        self.assertEqual(self.runs(), "xx")

    def test_setup_skipped_without_a_command(self):
        [n] = self.slot()
        self.assertEqual(self.run_setup(n), "skipped")
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_runs_under_heavy_prefix(self):
        marker = self.tmp / "marker.txt"
        script = self.tmp / "lock.py"
        script.write_text(
            "import subprocess, sys\n"
            f"open({json.dumps(str(marker))}, 'a', encoding='utf-8').write('locked\\n')\n"
            "sys.exit(subprocess.run(sys.argv[1:]).returncode)\n", encoding="utf-8", newline="\n")
        [n] = self.slot(1, **{"commands.setup": self.counter_cmd(), "commands.heavy_lock": [sys.executable, str(script)]})
        self.assertEqual(self.run_setup(n), "ok")
        self.assertEqual(marker.read_text(encoding="utf-8"), "locked\n")
        self.assertEqual(self.runs(), "x")

    def test_setup_failure_clears_hash(self):
        [n] = self.slot(1, **{"commands.setup": py_cmd("import sys; sys.exit(3)")})
        self.board.set_slot_setup_hash(n, "stale")
        self.assertEqual(self.run_setup(n), "failed")
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_that_cannot_start_is_a_failure_with_a_reason(self):
        tid = self.new_task()
        [n] = self.slot(1, **{"commands.setup": [str(self.tmp / "no-such-tool")]})
        self.board.set_slot_setup_hash(n, "stale")
        self.assertEqual(self.run_setup(n, task_id=tid), "failed")   # resolve_argv: the executable is not on PATH
        (run,) = self.board.list_runs(tid)
        self.assertEqual((run.outcome, run.exit_code), ("failed", None))
        self.assertIn("setup could not start", (self.repo.logs_dir / str(tid) / "setup-1.log").read_text(encoding="utf-8"))
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_timeout(self):
        [n] = self.slot(1, **{"commands.setup": py_cmd("import time; time.sleep(30)"), "setup_timeout": "1s",
                              "kill_grace": "1s"})
        self.board.set_slot_setup_hash(n, "stale")
        self.assertEqual(self.run_setup(n), "timeout")
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_stopped(self):
        import threading
        stop = threading.Event()
        stop.set()
        [n] = self.slot(1, **{"commands.setup": py_cmd("import time; time.sleep(30)"), "kill_grace": "1s"})
        self.assertEqual(self.run_setup(n, stop_event=stop), "stopped")
        self.assertIsNone(self.board.list_slots()[0].setup_hash)

    def test_setup_records_a_run_and_a_log_for_a_task(self):
        tid = self.new_task()
        [n] = self.slot(1, **{"commands.setup": py_cmd("import sys\nprint('hello')\nprint('oops', file=sys.stderr)\n")})
        self.assertEqual(self.run_setup(n, task_id=tid), "ok")
        (run,) = self.board.list_runs(tid)
        self.assertEqual((run.stage, run.n, run.outcome, run.exit_code, run.slot), ("setup", 1, "ok", 0, n))
        log = self.repo.logs_dir / str(tid) / "setup-1.log"
        self.assertEqual(Path(run.log_path), log)
        self.assertCountEqual(log.read_text(encoding="utf-8").splitlines(), ["hello", "oops"])   # stderr merged
        self.board.set_slot_setup_hash(n, None)
        self.assertEqual(self.run_setup(n, task_id=tid), "ok")
        self.assertEqual([r.n for r in self.board.list_runs(tid)], [1, 2])
        self.assertTrue((self.repo.logs_dir / str(tid) / "setup-2.log").exists())

    def test_setup_for_base_job_logs_under_base(self):
        [n] = self.slot(1, **{"commands.setup": py_cmd("print('base')")})
        self.assertEqual(self.run_setup(n, task_id=None), "ok")
        self.assertEqual((self.repo.logs_dir / "base" / "setup-1.log").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(self.board.all_runs(), [])   # no runs row (PD-34)
        self.board.set_slot_setup_hash(n, None)
        self.run_setup(n, task_id=None)
        self.assertTrue((self.repo.logs_dir / "base" / "setup-2.log").exists())

    def test_setup_on_start_reports_kind_setup(self):
        seen = []
        [n] = self.slot(1, **{"commands.setup": py_cmd("pass")})
        self.assertEqual(self.run_setup(n, on_start=lambda *a: seen.append(a)), "ok")
        self.assertEqual(len(seen), 1)
        kind, pid, group, fingerprint = seen[0]
        self.assertEqual((kind, group), ("setup", pid))

    def test_setup_env_is_passed(self):
        out = self.tmp / "env.txt"
        code = f"import os\nopen({json.dumps(str(out))}, 'w', encoding='utf-8').write(os.environ.get('ARTEL_PROBE', ''))\n"
        [n] = self.slot(1, **{"commands.setup": py_cmd(code)})
        env = dict(os.environ, ARTEL_PROBE="42")
        self.assertEqual(self.run_setup(n, env=env), "ok")
        self.assertEqual(out.read_text(encoding="utf-8"), "42")

    def test_setup_changed_detects_input_change(self):
        cfg = self.make_cfg(**{"commands.setup": py_cmd("pass"), "commands.setup_inputs": ["lock.txt"]})
        d = self.tmp / "s"
        d.mkdir()
        (d / "lock.txt").write_text("a", encoding="utf-8")
        stored = slots.setup_hash(d, cfg["commands.setup"], cfg["commands.setup_inputs"])
        self.assertFalse(slots.setup_changed(d, cfg, stored))
        (d / "lock.txt").write_text("b", encoding="utf-8")
        self.assertTrue(slots.setup_changed(d, cfg, stored))
        self.assertTrue(slots.setup_changed(d, cfg, None))
        other = self.make_cfg(**{"commands.setup": py_cmd("print(1)"), "commands.setup_inputs": ["lock.txt"]})
        (d / "lock.txt").write_text("a", encoding="utf-8")
        self.assertTrue(slots.setup_changed(d, other, stored))   # the command changed


class GuardedCallsTest(unittest.TestCase):
    def test_every_scrubbing_function_calls_guard_first(self):
        # the acceptance rule: no reset/clean/switch/merge without guard()
        import ast
        source = (Path(__file__).resolve().parents[1] / "artel" / "slots.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        risky = {"reset", "clean", "switch", "merge"}
        for func in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            git_verbs = {a.value for call in ast.walk(func) if isinstance(call, ast.Call)
                         for a in call.args if isinstance(a, ast.Constant) and a.value in risky}
            if not git_verbs or func.name == "scrub":
                continue
            names = [c.func.id for c in ast.walk(func) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
            self.assertIn("guard", names, func.name)
        scrub = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "scrub")
        self.assertEqual(ast.get_docstring(scrub), "Private: only called after guard().")


if __name__ == "__main__":
    unittest.main()

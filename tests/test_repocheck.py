import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from artel import board as B, config as C, platform as P, repocheck, slots
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
        """Mutation caught: watching refs/heads/artel/ only (a stash or a replace ref survives the stage), or the
        stash being set back (owner decision: it is reported, never touched)."""
        before = self.take()
        (self.slot1 / "README").write_text("changed\n", encoding="utf-8", newline="\n")
        git(self.slot1, "stash", "push", "-q")
        git(self.repo_path, "replace", self.sha[7], git(self.repo_path, "rev-parse", "main"))
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(len(changes), 2, changes)
        self.assertTrue(any(c.startswith("ref refs/stash ") for c in changes), changes)
        self.assertTrue(any(c.startswith("ref refs/replace/") for c in changes), changes)
        left = repocheck.restore(self.repo, before, after, changes)
        self.assertEqual(left, ["ref refs/stash created"])   # reported, not restored
        self.assertEqual(git(self.repo_path, "for-each-ref", "refs/replace/"), "")

    def test_human_stash_survives(self):
        """Mutation caught: `update-ref -d refs/stash` (the human's stash, with its reflog, is gone)."""
        (self.repo_path / "README").write_text("mine\n", encoding="utf-8", newline="\n")
        git(self.repo_path, "stash", "push", "-q")
        before = self.take()
        (self.repo_path / "README").write_text("again\n", encoding="utf-8", newline="\n")
        git(self.repo_path, "stash", "push", "-q")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["ref refs/stash moved"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), changes)
        self.assertEqual(git(self.repo_path, "stash", "list").count("\n"), 1)   # both entries still there

    def test_symref_restore_leaves_main_alone(self):
        """Mutation caught: a restore that follows a symbolic ref (`update-ref -d` on a symref to main deletes
        main; a symref to artel/7 rewinds artel/7)."""
        before = self.take()
        git(self.repo_path, "symbolic-ref", "refs/heads/artel/evil", "refs/heads/main")
        git(self.repo_path, "branch", "-D", "artel/8")
        git(self.repo_path, "symbolic-ref", "refs/heads/artel/8", "refs/heads/artel/7")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(sorted(changes), ["ref refs/heads/artel/8 moved", "ref refs/heads/artel/evil created"])
        main = git(self.repo_path, "rev-parse", "main")
        seven = git(self.repo_path, "rev-parse", "artel/7")
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "rev-parse", "main"), main)
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/7"), seven)
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/8"), self.sha[8])
        self.assertEqual(git(self.repo_path, "symbolic-ref", "-q", "refs/heads/artel/8", check=False), "")
        self.assertFalse(self.repo.branch_exists("artel/evil"))

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


class ConfigAndHooksTest(RepoCheckCase):
    def append_config(self, text):
        with open(self.repo.common_dir / "config", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def test_indented_section_header_is_a_change(self):
        """Mutation caught: a text/regex comparison of the config (an indented `[core]` after a runner-looking
        `[branch "artel/x"]` section hid core.hooksPath)."""
        before = self.take()
        self.append_config('[branch "artel/x"]\n\tremote = .\n  [core]\n\thooksPath = /evil/hooks\n')
        self.assertEqual(self.compare(before, self.take()), ["file config changed"])
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath"), "/evil/hooks")

    def test_config_restore_keeps_runner_branch_entries(self):
        """Mutation caught: a restore that writes the old bytes over what the runner wrote meanwhile
        (`switch -C` records branch.artel/N.*), or one that keeps the agent's change."""
        before = self.take()
        git(self.repo_path, "config", "core.hooksPath", "/evil")
        git(self.repo_path, "config", "branch.artel/9.remote", ".")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["file config changed"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath", check=False), "")
        self.assertEqual(git(self.repo_path, "config", "--get", "branch.artel/9.remote"), ".")
        self.assertEqual(self.compare(before, self.take()), [])

    def test_config_lock_skips_and_reports(self):
        """Mutation caught: rewriting the config while git holds config.lock."""
        before = self.take()
        git(self.repo_path, "config", "core.hooksPath", "/evil")
        after = self.take()
        (self.repo.common_dir / "config.lock").write_bytes(b"")
        self.assertEqual(repocheck.restore(self.repo, before, after, ["file config changed"]),
                         ["file config changed"])
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath"), "/evil")

    def test_other_worktree_config_is_watched(self):
        """Mutation caught: only the slot's own config.worktree watched."""
        git(self.repo_path, "config", "extensions.worktreeConfig", "true")
        before = self.take()
        git(self.slot2, "config", "--worktree", "core.hooksPath", "x")
        changes = self.compare(before, self.take())
        self.assertEqual(len(changes), 1, changes)
        self.assertIn("config.worktree", changes[0])

    def test_hook_mode_change_and_deleted_executable_hook(self):
        """Mutation caught: modes not in the snapshot (`chmod -x` undetected; a deleted executable hook comes
        back 0644 and is silently disabled)."""
        hook = self.repo.common_dir / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_bytes(b"#!/bin/sh\nexit 0\n")
        P.set_file_mode(hook, 0o755)
        if P.file_mode(hook) is None:
            self.skipTest("hook permission bits are a POSIX notion")
        before = self.take()
        P.set_file_mode(hook, 0o644)
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["file hooks/pre-commit changed"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(P.file_mode(hook), 0o755)
        hook.unlink()
        gone = self.take()
        self.assertEqual(repocheck.restore(self.repo, before, gone, self.compare(before, gone)), [])
        self.assertEqual(P.file_mode(hook), 0o755)

    def test_changed_file_is_copied_to_quarantine_before_it_is_rewritten(self):
        """Mutation caught: restore destroying the only copy of a human's edit (owner decision)."""
        before = self.take()
        attrs = self.repo.common_dir / "info" / "attributes"
        attrs.write_bytes(b"* text\n")
        after = self.take()
        quarantine = self.tmp / "q"
        self.assertEqual(repocheck.restore(self.repo, before, after, self.compare(before, after),
                                           quarantine=quarantine), [])
        self.assertEqual((quarantine / "info" / "attributes").read_bytes(), b"* text\n")
        self.assertFalse(attrs.exists())

    def test_restore_runs_no_planted_hook(self):
        """Mutation caught: refs restored before files / git run with the agent's hooksPath or fsmonitor (a
        planted `reference-transaction` hook or fsmonitor command then runs in the runner's process)."""
        marker = self.tmp / "ran.txt"
        script = self.tmp / "plant.py"
        script.write_text(f"open({str(marker)!r}, 'a', encoding='utf-8').write('x')\n", encoding="utf-8")
        hooks = self.tmp / "evil-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(f'#!/bin/sh\n"{sys.executable}" "{script.as_posix()}"\n', encoding="utf-8", newline="\n")
        P.set_file_mode(hook, 0o755)
        before = self.take(idle=[(2, self.slot2)])
        git(self.repo_path, "config", "core.hooksPath", str(hooks))
        git(self.repo_path, "config", "core.fsmonitor", f'"{sys.executable}" "{script.as_posix()}"')
        git(self.repo_path, "branch", "-D", "artel/8")
        marker.unlink(missing_ok=True)   # the agent's own `branch -D` ran the hook; only the runner's calls count
        after = self.take(idle=[(2, self.slot2)])
        changes = self.compare(before, after)
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertFalse(marker.exists(), "a planted hook or fsmonitor ran inside the check")
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/8"), self.sha[8])


class RoundTwoTest(RepoCheckCase):
    def marker_hook(self, directory: Path, name: str, marker: Path) -> Path:
        script = self.tmp / "plant2.py"
        script.write_text(f"open({str(marker)!r}, 'a', encoding='utf-8').write('x')\n", encoding="utf-8")
        directory.mkdir(parents=True, exist_ok=True)
        hook = directory / name
        hook.write_text(f'#!/bin/sh\n"{sys.executable}" "{script.as_posix()}"\n', encoding="utf-8", newline="\n")
        P.set_file_mode(hook, 0o755)
        return hook

    def test_planted_file_in_override_hooks_dir_never_runs(self):
        """Mutation caught: a fixed, agent-writable directory as the hooks override (a `reference-transaction`
        written there runs in the runner at restore; so does a file planted in any directory under artel/)."""
        marker = self.tmp / "ran-override.txt"
        before = self.take()
        git(self.repo_path, "branch", "-D", "artel/8")
        for name in ("empty-hooks", "tmp", "no-hooks"):   # every place an override directory could have been
            self.marker_hook(self.repo.artel_dir / name, "reference-transaction", marker)
        marker.unlink(missing_ok=True)
        after = self.take()
        self.assertEqual(repocheck.restore(self.repo, before, after, self.compare(before, after)), [])
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.repo_path, "rev-parse", "artel/8"), self.sha[8])

    def test_stale_config_lock_is_removed_and_restored(self):
        """Mutation caught: a `config.lock` the agent left blocking the restore (its hooksPath then stays)."""
        before = self.take()
        git(self.repo_path, "config", "core.hooksPath", "/evil")
        (self.repo.common_dir / "config.lock").write_bytes(b"")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(sorted(changes), ["file config changed", "file config.lock created"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertFalse((self.repo.common_dir / "config.lock").exists())
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath", check=False), "")

    def test_a_lock_that_vanished_is_not_a_change(self):
        """Mutation caught: a lock present in `before` and gone later reported as a deleted file (restore would
        recreate someone else's lock)."""
        (self.repo.common_dir / "config.lock").write_bytes(b"")
        before = self.take()
        (self.repo.common_dir / "config.lock").unlink()
        self.assertEqual(self.compare(before, self.take()), [])

    def test_a_lock_that_was_there_before_blocks_and_is_reported(self):
        """Mutation caught: removing a live git's lock, or claiming a restore that did not happen."""
        (self.repo.common_dir / "config.lock").write_bytes(b"")
        before = self.take()
        with open(self.repo.common_dir / "config", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[core]\n\thooksPath = /evil\n")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["file config changed"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), changes)
        self.assertEqual(repocheck.config_left(changes), changes)
        self.assertTrue((self.repo.common_dir / "config.lock").exists())

    def test_config_restore_drops_sections_runner_deleted(self):
        """Mutation caught: the old bytes written back whole (a `branch -D` of the runner is undone and the
        `[branch "artel/9"]` section comes back)."""
        git(self.repo_path, "config", "branch.artel/9.remote", ".")
        git(self.repo_path, "config", "branch.artel/9.merge", "refs/heads/x")
        before = self.take()
        git(self.repo_path, "config", "core.hooksPath", "/evil")
        git(self.repo_path, "config", "--remove-section", "branch.artel/9")
        git(self.repo_path, "config", "branch.artel/10.remote", ".")
        after = self.take()
        self.assertEqual(repocheck.restore(self.repo, before, after, self.compare(before, after)), [])
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath", check=False), "")
        self.assertEqual(git(self.repo_path, "config", "--get", "branch.artel/9.remote", check=False), "")
        self.assertEqual(git(self.repo_path, "config", "--get", "branch.artel/10.remote"), ".")

    def test_branch_merge_options_is_a_change(self):
        """Mutation caught: every branch.artel/* key exempt (`mergeOptions = -s ours` makes the runner's merges
        drop other tasks' work); only `.remote` and `.merge` are the runner's."""
        before = self.take()
        git(self.repo_path, "config", "branch.artel/9.remote", ".")
        git(self.repo_path, "config", "branch.artel/9.merge", "refs/heads/x")
        self.assertEqual(self.compare(before, self.take()), [])
        git(self.repo_path, "config", "branch.artel/9.mergeOptions", "-s ours")
        self.assertEqual(self.compare(before, self.take()), ["file config changed"])

    def test_dangling_symref_is_a_change(self):
        """Mutation caught: for-each-ref alone (a dangling symbolic ref is not listed; the runner's later
        `git branch artel/10` would create the target)."""
        before = self.take()
        git(self.repo_path, "symbolic-ref", "refs/heads/artel/10", "refs/heads/artel/nothere")
        after = self.take()
        changes = self.compare(before, after)
        self.assertEqual(changes, ["ref refs/heads/artel/10 created"])
        self.assertEqual(repocheck.restore(self.repo, before, after, changes), [])
        self.assertEqual(git(self.repo_path, "symbolic-ref", "-q", "refs/heads/artel/10", check=False), "")

    def test_planted_tmp_name_cannot_block_restore(self):
        """Mutation caught: a fixed temp name (a directory at `config.artel-tmp` or `hooks/x.artel-tmp` made the
        restore fail), and a quarantine copy that can fail the restore."""
        hook = self.repo.common_dir / "hooks" / "pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_bytes(b"#!/bin/sh\nexit 0\n")
        before = self.take()
        hook.write_bytes(b"#!/bin/sh\nexit 1\n")
        git(self.repo_path, "config", "core.hooksPath", "/evil")
        (self.repo.common_dir / "config.artel-tmp").mkdir()
        (self.repo.common_dir / "hooks" / "pre-push.artel-tmp").mkdir()
        quarantine = self.tmp / "q-blocked"
        quarantine.write_bytes(b"a file where the directory should be")
        after = self.take()
        self.assertEqual(repocheck.restore(self.repo, before, after, self.compare(before, after),
                                           quarantine=quarantine), [])
        self.assertEqual(hook.read_bytes(), b"#!/bin/sh\nexit 0\n")
        self.assertEqual(git(self.repo_path, "config", "--get", "core.hooksPath", check=False), "")

    def test_restore_leaves_no_temp_file_behind(self):
        """Mutation caught: a failed or finished replace leaving `<name>.<random>` files in hooks/ and info/."""
        before = self.take()
        attrs = self.repo.common_dir / "info" / "attributes"
        attrs.write_bytes(b"x\n")
        info = self.repo.common_dir / "info"
        names = {p.name for p in info.iterdir()}
        after = self.take()
        repocheck.restore(self.repo, before, after, self.compare(before, after))
        self.assertEqual({p.name for p in info.iterdir()}, names - {"attributes"})

    def test_parallel_restore_is_not_undone(self):
        """Mutation caught: no skip_files (stage B, whose before held stage A's planted state, puts it back after
        A's restore removed it)."""
        git(self.repo_path, "config", "core.hooksPath", "/evil-from-A")   # A's plant, present in B's before
        git(self.repo_path, "branch", "artel/evil", "main")
        before_b = self.take()
        git(self.repo_path, "config", "--unset", "core.hooksPath")        # A's restore
        git(self.repo_path, "branch", "-D", "artel/evil")
        after_b = self.take()
        changes = self.compare(before_b, after_b)
        self.assertEqual(sorted(changes), ["file config changed", "ref refs/heads/artel/evil deleted"])
        self.assertEqual(self.compare(before_b, after_b, skip_refs={"refs/heads/artel/evil"},
                                      skip_files={"config"}), [])

    def test_stash_deletion_names_the_sha(self):
        """Mutation caught: a FAIL text that cannot recover a dropped stash (`git stash store <sha>`)."""
        (self.repo_path / "README").write_text("mine\n", encoding="utf-8", newline="\n")
        git(self.repo_path, "stash", "push", "-q")
        sha = git(self.repo_path, "rev-parse", "refs/stash")
        before = self.take()
        git(self.repo_path, "stash", "clear")
        (change,) = self.compare(before, self.take())
        self.assertIn(sha[:12], change)

    def test_diff_external_and_foreign_git_dir_do_not_run_or_redirect(self):
        """Mutation caught: `--no-ext-diff` missing from tracked_state (a planted `diff.external` runs), or the
        inherited GIT_DIR reaching the check's git calls (they would read another repository)."""
        marker = self.tmp / "ext.txt"
        script = self.tmp / "ext.py"
        script.write_text(f"open({str(marker)!r}, 'a', encoding='utf-8').write('x')\n", encoding="utf-8")
        git(self.repo_path, "config", "diff.external", f'"{sys.executable}" "{script.as_posix()}"')
        (self.slot1 / "README").write_text("edited\n", encoding="utf-8", newline="\n")
        repocheck.tracked_state(self.repo, self.slot1)
        self.assertFalse(marker.exists())
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            snapshot = repocheck.take(self.repo, self.slot1)
        self.assertIn("refs/heads/artel/7", snapshot.refs)


class SkipWorktreeTest(RepoCheckCase):
    def test_skip_worktree_edit_is_caught(self):
        """Mutation caught: a tracked-file check by `git diff` alone (skip-worktree hides the edit and the flag
        survives into the next task's stage)."""
        before = repocheck.tracked_state(self.repo, self.slot1)
        (self.slot1 / "README").write_text("edited\n", encoding="utf-8", newline="\n")
        git(self.slot1, "update-index", "--skip-worktree", "README")
        self.assertEqual(git(self.slot1, "status", "--porcelain"), "")   # git itself shows nothing
        self.assertTrue(repocheck.restore_tracked(self.repo, self.slot1, before))
        self.assertEqual((self.slot1 / "README").read_text(encoding="utf-8"), "seed\n")
        self.assertEqual(git(self.slot1, "ls-files", "-v"), "H README")
        self.assertFalse(repocheck.restore_tracked(self.repo, self.slot1, before))

    def test_assume_unchanged_is_caught_and_leave_slot_clears_flags(self):
        """Mutation caught: the scrub of a slot leaving the flag set."""
        before = repocheck.tracked_state(self.repo, self.slot1)
        git(self.slot1, "update-index", "--assume-unchanged", "README")
        self.assertNotEqual(repocheck.tracked_state(self.repo, self.slot1), before)
        slots.leave_slot(self.repo, self.board, self.cfg, 1)
        self.assertEqual(repocheck.tracked_state(self.repo, self.slot1), before)


class UntrackedAllTest(RepoCheckCase):
    def test_status_show_untracked_files_no_does_not_hide_idle_slot_writes(self):
        """Mutation caught: `status` honouring `status.showUntrackedFiles=no` (a new directory in an idle slot)."""
        git(self.repo_path, "config", "status.showUntrackedFiles", "no")
        before = self.take(idle=[(2, self.slot2)])
        (self.slot2 / "newdir").mkdir()
        (self.slot2 / "newdir" / "x.txt").write_text("x\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.compare(before, self.take(idle=[(2, self.slot2)])), ["idle slot 2 changed"])


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

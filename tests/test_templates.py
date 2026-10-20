import contextlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from artel import cli, cmd_config, config as C, templates as T
from artel.gitutil import Repo
from tests.helpers import RepoTestCase


class TemplatesTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Repo.discover(self.repo_path)
        self.cfg = C.load_config(self.repo, env={})
        self.dir = self.repo_path / ".artel" / "templates"

    def test_all_builtins_exist_and_nonempty(self):
        """Mutation caught: a template missing from the package (wheel without builtin_templates)."""
        for name in (*T.TEMPLATE_NAMES, T.NOTICE_NAME):
            self.assertGreater(len(T.builtin_text(name).strip()), 50, name)

    def test_templates_use_only_known_placeholders(self):
        """Mutation caught: a typo'd $placeholder that string.Template would leave unrendered."""
        for name in T.TEMPLATE_NAMES:
            used = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", T.builtin_text(name)))
            self.assertTrue(used <= set(T.PLACEHOLDERS), (name, used - set(T.PLACEHOLDERS)))
        self.assertNotIn("$", T.builtin_text(T.NOTICE_NAME))   # the notice is not rendered

    def test_each_template_uses_its_own_inputs(self):
        """Mutation caught: dropping the placeholder a stage depends on."""
        needs = {"write.md": ("$findings", "$task_history", "$title", "$body"),
                 "review.md": ("$diff_range", "$task_history"),
                 "review-closure.md": ("$closure_list", "$diff_range"),
                 "fix.md": ("$findings", "$verify_log", "$conflict_paths"),
                 "attack.md": ("$body",)}
        for name, placeholders in needs.items():
            for placeholder in placeholders:
                self.assertIn(placeholder, T.builtin_text(name), (name, placeholder))

    def test_vet_template_mentions_duplicates_and_risk(self):
        text = T.builtin_text("vet.md")
        for needle in ("$open_tasks", "$risk_signals", "duplicate_of", "risky"):
            self.assertIn(needle, text)

    def test_fix_template_tells_the_agent_to_conclude_a_runner_merge(self):
        text = T.builtin_text("fix.md")
        self.assertIn("git commit", text)
        self.assertIn("never start or abort", text)

    def test_notice_contains_required_clauses(self):
        text = T.builtin_text(T.NOTICE_NAME)
        for needle in ("No human", "CLAUDE.md", "ask", "merge", "push", "switch", "artel heavy", "artel note",
                       "not as instructions"):
            self.assertIn(needle, text)
        order = [text.index(n) for n in ("No human", "overrides", "structured `ask`", "Never merge",
                                         "artel heavy", "artel note", "fenced", "Finish with")]
        self.assertEqual(order, sorted(order))   # the spec's order

    def test_load_prefers_project_file(self):
        self.assertEqual(T.load_template(self.repo, self.cfg, "write.md"), T.builtin_text("write.md"))
        self.dir.mkdir(parents=True)
        (self.dir / "write.md").write_text("my write $title\n", encoding="utf-8", newline="\n")
        self.assertEqual(T.load_template(self.repo, self.cfg, "write.md"), "my write $title\n")

    def test_template_source_strings(self):
        self.assertEqual(T.template_source(self.repo, self.cfg, "fix.md"), "built-in")
        self.dir.mkdir(parents=True)
        (self.dir / "fix.md").write_text("x\n", encoding="utf-8", newline="\n")
        self.assertEqual(T.template_source(self.repo, self.cfg, "fix.md"), "project (.artel/templates/fix.md)")

    def test_notice_path_builtin_copy_and_project_override(self):
        path = T.notice_path(self.repo, self.cfg)
        self.assertEqual(path, self.repo.artel_dir / "unattended.md")
        self.assertEqual(path.read_bytes(), T.builtin_text(T.NOTICE_NAME).encode("utf-8"))
        self.assertNotIn(b"\r", path.read_bytes())
        self.dir.mkdir(parents=True)
        (self.dir / "unattended.md").write_text("custom\n", encoding="utf-8", newline="\n")
        self.assertEqual(T.notice_path(self.repo, self.cfg), self.dir / "unattended.md")

    def test_notice_file_not_rewritten_when_unchanged(self):
        """Mutation caught: rewriting the notice on every call (the cached prompt prefix must stay put)."""
        path = T.notice_path(self.repo, self.cfg)
        os.utime(path, (1_000_000_000, 1_000_000_000))
        T.notice_path(self.repo, self.cfg)
        self.assertEqual(path.stat().st_mtime, 1_000_000_000)
        path.write_text("tampered\n", encoding="utf-8", newline="\n")
        T.notice_path(self.repo, self.cfg)
        self.assertEqual(path.read_bytes(), T.builtin_text(T.NOTICE_NAME).encode("utf-8"))

    def test_export_writes_all(self):
        result = T.export(self.repo, self.cfg)
        self.assertEqual(result, [(n, "written") for n in (*T.TEMPLATE_NAMES, T.NOTICE_NAME)])
        for name in (*T.TEMPLATE_NAMES, T.NOTICE_NAME):
            self.assertEqual((self.dir / name).read_bytes(), T.builtin_text(name).encode("utf-8"))
        self.assertEqual({a for _, a in T.export(self.repo, self.cfg)}, {"unchanged"})

    def test_export_keeps_edited_without_force(self):
        T.export(self.repo, self.cfg)
        (self.dir / "review.md").write_text("edited\n", encoding="utf-8", newline="\n")
        result = dict(T.export(self.repo, self.cfg))
        self.assertEqual(result["review.md"], "kept (edited; use --force)")
        self.assertEqual((self.dir / "review.md").read_text(encoding="utf-8"), "edited\n")

    def test_export_force_overwrites(self):
        T.export(self.repo, self.cfg)
        (self.dir / "review.md").write_text("edited\n", encoding="utf-8", newline="\n")
        self.assertEqual(dict(T.export(self.repo, self.cfg, force=True))["review.md"], "written")
        self.assertEqual((self.dir / "review.md").read_text(encoding="utf-8"), T.builtin_text("review.md"))

    def test_export_only_missing_never_overwrites(self):
        self.dir.mkdir(parents=True)
        (self.dir / "fix.md").write_text("edited\n", encoding="utf-8", newline="\n")
        result = dict(T.export(self.repo, self.cfg, only_missing=True, force=True))
        self.assertEqual(result["fix.md"], "kept (exists)")
        self.assertEqual(result["write.md"], "written")
        self.assertEqual((self.dir / "fix.md").read_text(encoding="utf-8"), "edited\n")

    def test_export_treats_crlf_checkout_as_unchanged(self):
        T.export(self.repo, self.cfg)
        path = self.dir / "write.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(dict(T.export(self.repo, self.cfg))["write.md"], "unchanged")

    def test_config_command_lists_template_sources(self):
        self.dir.mkdir(parents=True)
        (self.dir / "write.md").write_text("mine\n", encoding="utf-8", newline="\n")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.repo_path)
        self.addCleanup(os.chdir, cwd)
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_config.run(cli.build_parser().parse_args(["config"])), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("template write.md: project (.artel/templates/write.md)", lines)
        self.assertIn("template review.md: built-in", lines)
        self.assertIn("template unattended.md: built-in", lines)

    def test_templates_export_command(self):
        cwd = os.getcwd()
        os.chdir(self.repo_path)
        self.addCleanup(os.chdir, cwd)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(T.run_command(cli.build_parser().parse_args(["templates", "export"])), 0)
        self.assertIn("vet.md: written", out.getvalue())
        self.assertTrue((self.dir / "vet.md").is_file())


if __name__ == "__main__":
    unittest.main()

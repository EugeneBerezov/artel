import contextlib
import io
import sys
import types
import unittest

from artel import cli
from artel.errors import UsageError


class ParserTest(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def fails(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.parse(*argv)
        return cm.exception.code

    def test_add_parses_all_flags(self):
        a = self.parse("add", "Fix x", "--body", "b", "--area", "src/a", "--area", "docs",
                       "--priority", "3", "--after", "1", "--after", "2",
                       "--attack", "--strong", "--review", "3", "--merge", "ask")
        self.assertEqual((a.title, a.body, a.area, a.priority, a.after), ("Fix x", "b", ["src/a", "docs"], 3, [1, 2]))
        self.assertTrue(a.attack and a.strong)
        self.assertEqual((a.review, a.merge), (3, "ask"))

    def test_add_defaults(self):
        a = self.parse("add", "T")
        self.assertEqual((a.priority, a.area, a.after, a.attack, a.review, a.merge),
                         (0, [], [], False, None, None))

    def test_note_kinds(self):
        a = self.parse("note", "FAIL", "x", "--basis", "b")
        self.assertEqual((a.kind, a.text, a.basis), ("FAIL", "x", "b"))
        self.assertEqual(self.fails("note", "FINDING", "x"), 2)

    def test_heavy_strips_double_dash(self):
        self.assertEqual(self.parse("heavy", "--", "npx", "vitest", "run").cmd, ["npx", "vitest", "run"])

    def test_rework_requires_note(self):
        self.assertEqual(self.fails("rework", "1"), 2)
        self.assertEqual(self.parse("rework", "1", "--note", "n").note, "n")

    def test_config_forms(self):
        self.assertIsNone(self.parse("config").config_cmd)
        a = self.parse("config", "explain", "models.write")
        self.assertEqual((a.config_cmd, a.key), ("explain", "models.write"))
        a = self.parse("config", "set", "max_agents", "3")
        self.assertEqual((a.config_cmd, a.key, a.value), ("set", "max_agents", "3"))

    def test_templates_export_force(self):
        a = self.parse("templates", "export", "--force")
        self.assertEqual((a.templates_cmd, a.force), ("export", True))

    def test_simple_commands(self):
        self.assertEqual(self.parse("run", "-n", "2").n, 2)
        self.assertTrue(self.parse("stop", "--now").now)
        self.assertTrue(self.parse("resume", "--force").force)
        self.assertTrue(self.parse("init", "--yes").yes)
        self.assertTrue(self.parse("stats", "--json").json)
        self.assertEqual(self.parse("notify", "test").notify_cmd, "test")
        self.assertEqual(self.parse("reject", "4", "--reason", "r").reason, "r")
        self.assertEqual(self.parse("answer", "4", "yes").id, 4)

    def test_unknown_command_is_usage_error(self):
        self.assertEqual(self.fails("frobnicate"), 2)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with self.assertRaises(SystemExit) as cm:
                self.parse("--version")
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("0.1.0", out.getvalue())


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(cli.COMMANDS)

    def tearDown(self):
        cli.COMMANDS.clear()
        cli.COMMANDS.update(self._saved)

    def test_missing_handler_returns_3(self):
        cli.COMMANDS["status"] = "artel.does_not_exist:run"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["status"]), 3)
        self.assertIn("artel status: not available in this build", err.getvalue())

    def test_artel_error_maps_to_exit_code(self):
        def boom(args):
            raise UsageError("x")
        mod = types.ModuleType("artel_test_boom")
        mod.run = boom
        sys.modules["artel_test_boom"] = mod
        self.addCleanup(sys.modules.pop, "artel_test_boom", None)
        cli.COMMANDS["status"] = "artel_test_boom:run"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["status"]), 2)
        self.assertEqual(err.getvalue().strip(), "artel: x")

    def test_every_command_has_parser(self):
        sub = next(a for a in cli.build_parser()._actions if a.choices and "init" in a.choices)
        self.assertEqual(set(sub.choices), set(cli.COMMANDS))

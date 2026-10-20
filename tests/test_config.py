import argparse
import contextlib
import io
import os
import tomllib
import unittest
from pathlib import Path

from artel import cmd_config, config as C, config_schema as S
from artel.errors import UsageError
from artel.gitutil import Repo
from tests.helpers import RepoTestCase, git

EXPECTED_DEFAULTS = {
    "base": "main", "max_agents": 2, "max_attempts": 2, "review_rounds": 2, "max_verify_fixes": 2,
    "merge": "auto", "stage_timeout": "90m", "setup_timeout": "20m", "verify_timeout": "30m",
    "kill_grace": "30s", "context_budget": 8000, "limit_stop": "allowed_warning", "min_free_disk": "3G",
    "slots_dir": ".claude/worktrees", "slot_keep": ["node_modules"], "log_keep_tasks": 50,
    "poll_interval": "5s", "setup_retry": "15m", "autoapprove": "non_risky",
    "risk_signals": ["deletes data", "quotas or money", "protocol or public contract", "visible to end users"],
    "rotation.writer_max_tasks": 1, "rotation.writer_max_context": 0, "rotation.same_area_only": True,
    "rotation.fix_resumes_writer": True, "templates_dir": ".artel/templates", "agent_command": "claude",
    "shell": "auto", "models.write": "sonnet", "models.review": "sonnet", "models.strong": "opus",
    "models.strong_diff_lines": 300, "commands.setup": "", "commands.setup_inputs": [],
    "commands.verify": "", "commands.heavy_lock": "", "commands.heavy_lock_check": "",
    "commands.project_tools": [], "commands.project_read_tools": [],
    "profiles.common": ["-p", "--model", "{model}", "--output-format", "stream-json", "--verbose",
                        "--json-schema", "{schema}", "--permission-prompts", "none", "--disable-slash-commands",
                        "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections",
                        "--append-system-prompt-file", "{unattended_notice}"],
    "profiles.writer": ["--permission-mode", "acceptEdits", "--allowedTools",
                        "Bash(artel note *) Bash(artel add *) Bash(artel heavy *) Bash(git add *) "
                        "Bash(git commit *) Bash(git status *) Bash(git diff *)", "{project_tools}"],
    "profiles.reviewer": ["--permission-mode", "default", "--disallowedTools", "Edit Write NotebookEdit",
                          "--allowedTools", "Bash(artel note *) Bash(git diff *) Bash(git log *) "
                          "Bash(git show *) Bash(git status *)", "{project_read_tools}"],
    "notify.on": ["blocked", "stuck", "paused", "resumed", "runner_stopped", "error"],
    "notify.command": "", "notify.telegram_enabled": False, "notify.telegram_token_env": "ARTEL_TELEGRAM_TOKEN",
    "notify.telegram_token_file": "", "notify.telegram_chat_env": "ARTEL_TELEGRAM_CHAT",
    "notify.telegram_chat_file": "", "notify.telegram_api_base": "https://api.telegram.org",
}


def setting(key):
    return S.by_key(key)


class SchemaTest(unittest.TestCase):
    def test_keys_and_defaults_match_plan_table(self):
        # catches: a renamed/missing/extra key or a changed default
        self.assertEqual({s.key: s.default for s in S.SETTINGS}, EXPECTED_DEFAULTS)
        self.assertEqual(len(S.SETTINGS), len(EXPECTED_DEFAULTS))   # no duplicate keys

    def test_kinds_match_plan(self):
        kinds = {"base": "str", "max_agents": "int", "merge": "enum", "stage_timeout": "duration",
                 "limit_stop": "enum", "min_free_disk": "size", "slot_keep": "str_list",
                 "autoapprove": "enum", "rotation.same_area_only": "bool", "agent_command": "argv",
                 "shell": "enum", "commands.verify": "command", "commands.setup_inputs": "str_list",
                 "profiles.writer": "str_list", "notify.on": "enum_list", "notify.telegram_enabled": "bool"}
        for key, kind in kinds.items():
            self.assertEqual(setting(key).kind, kind, key)
        self.assertEqual(setting("rotation.writer_max_tasks").minimum, 1)
        self.assertEqual(setting("rotation.writer_max_context").minimum, 0)

    def test_profile_defaults_match_plan(self):
        common = setting("profiles.common").default
        self.assertIn("{schema}", common)
        self.assertIn("{unattended_notice}", common)
        self.assertNotIn("--setting-sources", common)

    def test_every_setting_has_meaning_and_explain(self):
        for s in S.SETTINGS:
            self.assertTrue(s.meaning.strip(), s.key)
            self.assertNotIn("\n", s.meaning, s.key)
            self.assertNotIn("\n", s.cost, s.key)
            self.assertTrue(s.explain.strip(), s.key)

    def test_cost_present_for_quota_settings(self):
        keys = ["max_agents", "max_attempts", "review_rounds", "max_verify_fixes", "stage_timeout",
                "limit_stop", "models.write", "models.review", "models.strong", "models.strong_diff_lines",
                "profiles.common", "profiles.writer", "profiles.reviewer"]
        for key in keys:
            self.assertTrue(setting(key).cost.strip(), key)
        self.assertEqual(setting("models.strong_diff_lines").cost, "Lower = more reviews on the strong model.")

    def test_rendered_toml_parses_back_to_defaults(self):
        for comments in (True, False):
            data = tomllib.loads(C.render_toml(comments=comments))
            for s in S.SETTINGS:
                table, _, name = s.key.rpartition(".")
                node = data[table] if table else data
                self.assertEqual(node[name], s.default, s.key)

    def test_rendered_toml_has_comment_on_every_key_line(self):
        for line in C.render_toml().splitlines():
            if "=" in line and not line.startswith("#"):
                self.assertIn("  # ", line)
        self.assertNotIn("#", C.render_toml(comments=False))

    def test_out_of_scope_keys_absent(self):
        keys = {s.key for s in S.SETTINGS}
        self.assertFalse({"integration", "propose_minors", "notify.done_digest", "done_digest"} & keys)

    def test_defaults_markdown_has_row_per_setting(self):
        md = S.defaults_markdown()
        for s in S.SETTINGS:
            self.assertIn(f"| `{s.key}` |", md)
        self.assertEqual(len(md.strip().splitlines()), len(S.SETTINGS) + 2)

    def test_both_profiles_pass_permission_mode(self):
        writer, reviewer = setting("profiles.writer").default, setting("profiles.reviewer").default
        self.assertEqual(writer[writer.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(reviewer[reviewer.index("--permission-mode") + 1], "default")
        for s in S.SETTINGS:
            self.assertNotIn("bypassPermissions", str(s.default), s.key)
            self.assertNotIn("bypassPermissions", s.explain, s.key)

    def test_rotation_and_vet_settings(self):
        self.assertIn("[rotation]", C.render_toml())
        self.assertIn("no threshold", setting("rotation.writer_max_context").explain)
        for key in ("rotation.writer_max_tasks", "rotation.writer_max_context", "rotation.same_area_only",
                    "rotation.fix_resumes_writer"):
            self.assertIn("Task 26", setting(key).explain, key)
        self.assertEqual(setting("autoapprove").choices, ("none", "non_risky", "all"))

    def test_notify_on_accepts_error_event(self):
        self.assertIn("error", setting("notify.on").choices)
        self.assertIn("error", setting("notify.on").default)
        self.assertNotIn("done", setting("notify.on").default)

    def test_by_key_unknown(self):
        with self.assertRaises(UsageError) as cm:
            S.by_key("nope")
        self.assertIn("unknown setting: nope", str(cm.exception))


class ParseTest(unittest.TestCase):
    def test_parse_duration(self):
        cases = {"90m": 5400.0, "30s": 30.0, "100ms": 0.1, "1.5s": 1.5, "1h": 3600.0, "2d": 172800.0,
                 45: 45.0, 2.5: 2.5}
        for raw, want in cases.items():
            self.assertEqual(C.parse_duration(raw), want, raw)
        for bad in ("5x", "", "m", True, -1, "abc", "inf", "nan", "1_0", float("inf"), float("nan")):
            with self.assertRaises(UsageError, msg=repr(bad)):
                C.parse_duration(bad)

    def test_parse_size(self):
        self.assertEqual(C.parse_size("3G"), 3 * 1024 ** 3)
        self.assertEqual(C.parse_size("500M"), 500 * 1024 ** 2)
        self.assertEqual(C.parse_size("1024K"), 1024 * 1024)
        self.assertEqual(C.parse_size("1.5g"), int(1.5 * 1024 ** 3))
        self.assertEqual(C.parse_size(1000), 1000)
        for bad in ("3X", "G", "", True, -5, 1.5):
            with self.assertRaises(UsageError, msg=repr(bad)):
                C.parse_size(bad)

    def test_coerce_int(self):
        self.assertEqual(C.coerce(setting("max_agents"), "3"), 3)
        for bad in ("x", "0", 0, True, 1.5, "1_0", "\u0663"):
            with self.assertRaises(UsageError, msg=repr(bad)):
                C.coerce(setting("max_agents"), bad)   # minimum is 1
        self.assertEqual(C.coerce(setting("rotation.writer_max_context"), "0"), 0)

    def test_coerce_lists_from_string(self):
        s = setting("slot_keep")
        self.assertEqual(C.coerce(s, "a, b ,c"), ["a", "b", "c"])
        self.assertEqual(C.coerce(s, '["a b", "c"]'), ["a b", "c"])
        self.assertEqual(C.coerce(s, ""), [])
        self.assertEqual(C.coerce(s, ["x"]), ["x"])
        with self.assertRaises(UsageError):
            C.coerce(s, "[1, 2]")
        with self.assertRaises(UsageError):
            C.coerce(s, "[unquoted")

    def test_coerce_enum_rejects_unknown(self):
        self.assertEqual(C.coerce(setting("merge"), "ask"), "ask")
        with self.assertRaises(UsageError) as cm:
            C.coerce(setting("merge"), "sometimes")
        self.assertIn("merge", str(cm.exception))
        with self.assertRaises(UsageError):
            C.coerce(setting("notify.on"), "blocked,exploded")
        self.assertEqual(C.coerce(setting("notify.on"), "blocked, error"), ["blocked", "error"])

    def test_coerce_bool(self):
        s = setting("rotation.same_area_only")
        for raw, want in (("true", True), ("False", False), ("yes", True), ("0", False), (True, True)):
            self.assertIs(C.coerce(s, raw), want, raw)
        for bad in ("maybe", 1, None):
            with self.assertRaises(UsageError, msg=repr(bad)):
                C.coerce(s, bad)

    def test_coerce_duration_and_size_give_runtime_values(self):
        self.assertEqual(C.coerce(setting("stage_timeout"), "90m"), 5400.0)
        self.assertEqual(C.coerce(setting("min_free_disk"), "3G"), 3 * 1024 ** 3)
        with self.assertRaises(UsageError) as cm:
            C.coerce(setting("stage_timeout"), "soon")
        self.assertIn("stage_timeout", str(cm.exception))

    def test_command_kind_string_or_list(self):
        s = setting("commands.verify")
        self.assertEqual(C.coerce(s, "npm test"), "npm test")
        self.assertEqual(C.coerce(s, '["npm", "test"]'), ["npm", "test"])
        self.assertEqual(C.coerce(s, ["npm", "test"]), ["npm", "test"])
        self.assertEqual(C.coerce(s, ""), "")
        self.assertEqual(C.coerce(s, []), "")
        self.assertEqual(C.coerce(s, "[]"), "")
        with self.assertRaises(UsageError):
            C.coerce(s, 5)

    def test_argv_string_is_never_split(self):
        # catches: shlex-splitting a string agent_command (PD-44) — eats Windows backslashes, splits spaces
        s = setting("agent_command")
        for path in ("C:\\Tools\\claude.exe", "/opt/my tools/claude", "claude"):
            self.assertEqual(C.coerce(s, path), path)
        self.assertEqual(C.coerce(s, '["claude", "--bare"]'), ["claude", "--bare"])
        for bad in ("", [], "[]"):
            with self.assertRaises(UsageError, msg=repr(bad)):
                C.coerce(s, bad)


class LoadTest(RepoTestCase):
    def repo(self):
        return Repo.discover(self.repo_path)

    def write_toml(self, text):
        (self.repo_path / "artel.toml").write_text(text, encoding="utf-8", newline="\n")

    def test_defaults_without_file(self):
        cfg = C.load_config(self.repo(), env={})
        self.assertEqual(cfg.path, self.repo().root / "artel.toml")
        self.assertEqual(set(cfg.values), {s.key for s in S.SETTINGS})
        for s in S.SETTINGS:
            self.assertEqual(cfg.source(s.key), "default", s.key)
        self.assertEqual(cfg["stage_timeout"], 5400.0)
        self.assertEqual(cfg["min_free_disk"], 3 * 1024 ** 3)
        self.assertEqual(cfg["commands.verify"], "")
        self.assertEqual(cfg["agent_command"], "claude")

    def test_sources_precedence(self):
        self.write_toml("max_agents = 3\n[models]\nreview = \"haiku\"\n")
        cfg = C.load_config(self.repo(), env={"ARTEL_MODELS_REVIEW": "opus"}, cli={"max_agents": (4, "flag -n")})
        self.assertEqual((cfg["max_agents"], cfg.source("max_agents")), (4, "flag -n"))
        self.assertEqual((cfg["models.review"], cfg.source("models.review")), ("opus", "env ARTEL_MODELS_REVIEW"))
        self.assertEqual((cfg["stage_timeout"], cfg.source("stage_timeout")), (5400, "default"))
        self.assertEqual(cfg.source("models.write"), "default")

    def test_file_beats_default_and_env_beats_file(self):
        self.write_toml("max_agents = 3\nmerge = \"ask\"\n")
        cfg = C.load_config(self.repo(), env={"ARTEL_MERGE": "auto"})
        self.assertEqual((cfg["max_agents"], cfg.source("max_agents")), (3, "artel.toml"))
        self.assertEqual((cfg["merge"], cfg.source("merge")), ("auto", "env ARTEL_MERGE"))

    def test_env_name_for_dotted_key(self):
        self.assertEqual(C.env_name("notify.telegram_enabled"), "ARTEL_NOTIFY_TELEGRAM_ENABLED")
        cfg = C.load_config(self.repo(), env={"ARTEL_NOTIFY_TELEGRAM_ENABLED": "true", "ARTEL_TASK": "7"})
        self.assertIs(cfg["notify.telegram_enabled"], True)

    def test_unknown_key_in_file_is_error(self):
        for text in ("bogus = 1\n", "[models]\nweird = 1\n", "[mystery]\nx = 1\n", "integration = \"base\"\n"):
            self.write_toml(text)
            with self.assertRaises(UsageError) as cm:
                C.load_config(self.repo(), env={})
            self.assertIn("artel.toml", str(cm.exception), text)
        self.write_toml("[models]\nweird = 1\n")
        with self.assertRaises(UsageError) as cm:
            C.load_config(self.repo(), env={})
        self.assertIn("models.weird", str(cm.exception))

    def test_invalid_value_names_key(self):
        self.write_toml("max_agents = \"many\"\n")
        with self.assertRaises(UsageError) as cm:
            C.load_config(self.repo(), env={})
        self.assertIn("max_agents", str(cm.exception))
        self.write_toml("")
        with self.assertRaises(UsageError) as cm:
            C.load_config(self.repo(), env={"ARTEL_STAGE_TIMEOUT": "soon"})
        self.assertIn("stage_timeout", str(cm.exception))

    def test_broken_toml_is_usage_error(self):
        self.write_toml("max_agents = [\n")
        with self.assertRaises(UsageError) as cm:
            C.load_config(self.repo(), env={})
        self.assertIn("artel.toml", str(cm.exception))

    def test_config_read_from_main_worktree_even_in_linked_worktree(self):
        self.write_toml("max_agents = 5\n")
        linked = self.tmp / "linked"
        git(self.repo_path, "worktree", "add", "-q", "-b", "side", str(linked))
        cfg = C.load_config(Repo.discover(linked), env={})
        self.assertEqual(cfg["max_agents"], 5)
        self.assertEqual(cfg.path, self.repo().root / "artel.toml")


class SetTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.repo_path / "artel.toml"

    def write(self, text):
        self.path.write_bytes(text.encode("utf-8"))

    def read(self):
        return self.path.read_bytes().decode("utf-8")

    def test_set_keeps_comment_and_other_lines(self):
        self.write("# top\nmax_agents = 2  # keep me\nmerge = \"auto\"\n\n[models]\nreview = \"sonnet\"  # r\n")
        C.set_value(self.path, "max_agents", "4")
        C.set_value(self.path, "models.review", "haiku")
        self.assertEqual(self.read(),
                         "# top\nmax_agents = 4  # keep me\nmerge = \"auto\"\n\n[models]\nreview = \"haiku\"  # r\n")

    def test_set_replaces_multiline_array_and_value_with_hash(self):
        self.write("slot_keep = [\n  \"a\",  # why\n  \"b#c\",\n]  # tail\nbase = \"x#y\"  # c\n")
        C.set_value(self.path, "slot_keep", "node_modules,dist")
        C.set_value(self.path, "base", "trunk")
        self.assertEqual(self.read(), "slot_keep = [\"node_modules\", \"dist\"]  # tail\nbase = \"trunk\"  # c\n")

    def test_set_adds_missing_key_under_table(self):
        self.write("max_agents = 2\n\n[models]\nreview = \"sonnet\"\n\n[notify]\non = [\"blocked\"]\n")
        C.set_value(self.path, "models.write", "haiku")
        C.set_value(self.path, "base", "trunk")
        C.set_value(self.path, "commands.verify", "make test")   # table absent: created
        text = self.read()
        data = tomllib.loads(text)
        self.assertEqual(data["models"], {"review": "sonnet", "write": "haiku"})
        self.assertEqual(data["base"], "trunk")
        self.assertEqual(data["commands"], {"verify": "make test"})
        self.assertEqual(data["notify"], {"on": ["blocked"]})
        self.assertLess(text.index("base"), text.index("[models]"))   # top-level key stays above the tables
        self.assertLess(text.index("write"), text.index("[notify]"))

    def test_set_creates_missing_file(self):
        C.set_value(self.path, "models.write", "haiku")
        self.assertEqual(tomllib.loads(self.read()), {"models": {"write": "haiku"}})

    def test_set_without_trailing_newline(self):
        self.write("max_agents = 2")
        C.set_value(self.path, "base", "trunk")
        self.assertEqual(tomllib.loads(self.read()), {"max_agents": 2, "base": "trunk"})

    def test_set_dotted_key_style(self):
        self.write("models.review = \"sonnet\"\n")
        C.set_value(self.path, "models.review", "haiku")
        self.assertEqual(self.read(), "models.review = \"haiku\"\n")

    def test_set_preserves_crlf(self):
        self.write("max_agents = 2\r\nmerge = \"auto\"\r\n")
        C.set_value(self.path, "max_agents", "3")
        self.assertEqual(self.read(), "max_agents = 3\r\nmerge = \"auto\"\r\n")

    def test_set_crlf_file_keeps_crlf_on_insert(self):
        self.write("max_agents = 2\r\n[models]\r\nreview = \"sonnet\"\r\n")
        C.set_value(self.path, "models.write", "haiku")
        C.set_value(self.path, "base", "trunk")
        C.set_value(self.path, "notify.command", "x")
        text = self.read()
        self.assertEqual(text.count("\n"), text.count("\r\n"))
        self.assertEqual(tomllib.loads(text)["models"]["write"], "haiku")

    def test_bom_file_is_read_and_kept(self):
        self.path.write_bytes(b"\xef\xbb\xbfmax_agents = 2\n")
        self.assertEqual(C.load_config(Repo.discover(self.repo_path), env={})["max_agents"], 2)
        C.set_value(self.path, "max_agents", "3")
        self.assertEqual(self.path.read_bytes(), b"\xef\xbb\xbfmax_agents = 3\n")

    def test_set_ignores_header_lookalike_inside_multiline_value(self):
        self.write("slot_keep = [\n  [\"x\"]\n]\n[models]\nreview = \"a\"\n")
        # nested array is not a str_list, but the scan must not take its middle line for a table header
        C.set_value(self.path, "models.write", "haiku")
        self.assertEqual(tomllib.loads(self.read())["models"], {"review": "a", "write": "haiku"})

    def test_set_invalid_value_leaves_file_unchanged(self):
        self.write("max_agents = 2\n")
        for key, raw in (("max_agents", "many"), ("merge", "sometimes"), ("stage_timeout", "5x")):
            with self.assertRaises(UsageError):
                C.set_value(self.path, key, raw)
        self.assertEqual(self.read(), "max_agents = 2\n")

    def test_set_file_that_would_not_parse_is_left_alone(self):
        broken = "max_agents = 2\nthis is not toml\n"
        self.write(broken)
        with self.assertRaises(UsageError):
            C.set_value(self.path, "base", "trunk")
        self.assertEqual(self.read(), broken)

    def test_set_unknown_key_errors(self):
        with self.assertRaises(UsageError):
            C.set_value(self.path, "nope", "1")
        self.assertFalse(self.path.exists())

    def test_set_windows_path_is_escaped(self):
        C.set_value(self.path, "commands.heavy_lock", "C:\\Tools\\lock.exe")
        self.assertIn('"C:\\\\Tools\\\\lock.exe"', self.read())
        self.assertEqual(tomllib.loads(self.read())["commands"]["heavy_lock"], "C:\\Tools\\lock.exe")
        self.assertNotIn(b"\r", self.path.read_bytes())
        C.set_value(self.path, "agent_command", "/opt/my tools/claude")
        cfg = C.load_config(Repo.discover(self.repo_path), env={})
        self.assertEqual(cfg["agent_command"], "/opt/my tools/claude")
        self.assertEqual(cfg["commands.heavy_lock"], "C:\\Tools\\lock.exe")

    def test_set_unicode_roundtrip_as_utf8(self):
        C.set_value(self.path, "base", "ветка")
        self.assertEqual(tomllib.loads(self.read())["base"], "ветка")
        self.assertIn("ветка".encode("utf-8"), self.path.read_bytes())

    def test_rendered_file_can_be_edited(self):
        self.write(C.render_toml())
        C.set_value(self.path, "profiles.common", '["-p", "--model", "{model}"]')
        C.set_value(self.path, "notify.on", "done")
        cfg = C.load_config(Repo.discover(self.repo_path), env={})
        self.assertEqual(cfg["profiles.common"], ["-p", "--model", "{model}"])
        self.assertEqual(cfg["notify.on"], ["done"])
        self.assertEqual(cfg["max_agents"], 2)


class CommandTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self.repo_path)
        self._env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("ARTEL_")}

    def tearDown(self):
        os.environ.update(self._env)
        os.chdir(self._cwd)
        super().tearDown()

    def run_cmd(self, *argv):
        args = argparse.Namespace(config_cmd=argv[0] if argv else None,
                                  key=argv[1] if len(argv) > 1 else None,
                                  value=argv[2] if len(argv) > 2 else None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_config.run(args)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_config_lists_every_key_with_source(self):
        lines = self.run_cmd().splitlines()
        self.assertEqual(len(lines), len(S.SETTINGS))
        for s, line in zip(S.SETTINGS, lines):
            self.assertTrue(line.startswith(f"{s.key} = "), line)
            self.assertIn("  (default)  — " + s.meaning, line)
        self.assertIn("stage_timeout = \"90m\"  (default)", "\n".join(lines))

    def test_explain_prints_long_text(self):
        out = self.run_cmd("explain", "models.strong_diff_lines")
        s = setting("models.strong_diff_lines")
        self.assertIn(s.key, out)
        self.assertIn("default: 300", out)
        self.assertIn("value:   300  (default)", out)
        self.assertIn(s.explain, out)
        self.assertIn(s.cost, out)

    def test_set_then_show_source_artel_toml(self):
        out = self.run_cmd("set", "max_agents", "3")
        self.assertIn("max_agents = 3  (artel.toml)", out)
        self.assertIn("max_agents = 3  (artel.toml)", self.run_cmd())
        self.assertIn("value:   3  (artel.toml)", self.run_cmd("explain", "max_agents"))
        os.environ["ARTEL_MAX_AGENTS"] = "6"
        try:
            self.assertIn("max_agents = 6  (env ARTEL_MAX_AGENTS)", self.run_cmd())
        finally:
            del os.environ["ARTEL_MAX_AGENTS"]

    def test_usage_errors(self):
        for argv in (("explain",), ("set", "max_agents"), ("explain", "nope"), ("set", "max_agents", "x")):
            with self.assertRaises(UsageError, msg=str(argv)):
                self.run_cmd(*argv)

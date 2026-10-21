import ast
import contextlib
import io
import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from artel import config as config_mod, config_schema, events, notify
from artel.config import load_config
from artel.events import Event
from artel.gitutil import Repo
from tests.fake_telegram import FakeTelegram
from tests.helpers import REPO_ROOT, RepoTestCase

TOKEN = "123456789:SECRET-TOKEN-abcdefghijklmnopqrstuvwxyz"
CHAT = "42"


class NotifyBase(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.server = FakeTelegram().start()
        self.addCleanup(self.server.stop)
        self.repo = Repo.discover(self.repo_path)
        self.env = {"ARTEL_TELEGRAM_TOKEN": TOKEN, "ARTEL_TELEGRAM_CHAT": CHAT}

    def toml(self, **values):
        lines = [f"[notify]", f'telegram_api_base = "{self.server.api_base}"']
        for key, value in values.items():
            lines.append(f"{key} = {json.dumps(value)}")
        (self.repo_path / "artel.toml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        self.cfg = load_config(self.repo, env={})
        return self.cfg

    def setup_cfg(self, **values):
        return self.toml(**values)


class CredentialsTest(NotifyBase):
    def test_credentials_lookup_order(self):
        """Mutation caught: any two sources swapped; file sources not stripped."""
        tf, cf = self.tmp / "token.txt", self.tmp / "chat.txt"
        tf.write_text("FILETOKEN\n", encoding="utf-8")
        cf.write_text("  777 \n", encoding="utf-8")
        cfg = self.toml(telegram_token_file=str(tf), telegram_chat_file=str(cf), telegram_token_env="MY_TOKEN",
                        telegram_chat_env="MY_CHAT")
        env = {"MY_TOKEN": "cfgenv", "MY_CHAT": "9", "ARTEL_TELEGRAM_TOKEN": "artel", "ARTEL_TELEGRAM_CHAT": "8",
               "TELEGRAM_BOT_TOKEN": "tg", "TELEGRAM_CHAT_ID": "7"}
        with self.subTest("config files first, stripped"):
            c = notify.find_telegram_credentials(cfg, env)
            self.assertEqual((c.token, c.chat), ("FILETOKEN", "777"))
            self.assertIn(f"file {tf}", c.source)
        tf.unlink(), cf.unlink()
        with self.subTest("config env names"):
            c = notify.find_telegram_credentials(cfg, env)
            self.assertEqual((c.token, c.chat), ("cfgenv", "9"))
            self.assertIn("env MY_TOKEN", c.source)
        del env["MY_TOKEN"], env["MY_CHAT"]
        with self.subTest("ARTEL_TELEGRAM_*"):
            c = notify.find_telegram_credentials(cfg, env)
            self.assertEqual((c.token, c.chat), ("artel", "8"))
        del env["ARTEL_TELEGRAM_TOKEN"], env["ARTEL_TELEGRAM_CHAT"]
        with self.subTest("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"):
            c = notify.find_telegram_credentials(cfg, env)
            self.assertEqual((c.token, c.chat), ("tg", "7"))
            self.assertIn("env TELEGRAM_BOT_TOKEN", c.source)
        with self.subTest("both are required"):
            self.assertIsNone(notify.find_telegram_credentials(cfg, {"TELEGRAM_BOT_TOKEN": "tg"}))
            self.assertIsNone(notify.find_telegram_credentials(cfg, {"TELEGRAM_CHAT_ID": "7"}))
            self.assertIsNone(notify.find_telegram_credentials(cfg, {}))

    def test_credentials_repr_hides_the_token(self):
        """Mutation caught: the dataclass repr (a traceback or a log line would carry the token)."""
        creds = notify.find_telegram_credentials(self.toml(), self.env)
        self.assertNotIn("SECRET-TOKEN", repr(creds))

    def test_missing_credentials_explains(self):
        """Mutation caught: silence (or a traceback) when nothing is configured."""
        out = io.StringIO()
        self.assertFalse(notify.check_and_enable(self.repo, self.toml(), {}, out))
        text = out.getvalue()
        for needle in ("ARTEL_TELEGRAM_TOKEN", "ARTEL_TELEGRAM_CHAT", "telegram_token_file"):
            self.assertIn(needle, text)
        self.assertEqual(self.server.paths, [])


class CheckTest(NotifyBase):
    def test_get_me_failure_disables_and_never_calls_get_updates(self):
        """Mutation caught: enabling after a failed getMe; any getUpdates call; printing the token."""
        self.server.responses["getMe"] = (401, {"ok": False, "description": "Unauthorized"})
        out = io.StringIO()
        ok = notify.check_and_enable(self.repo, self.toml(), self.env, out)
        self.assertFalse(ok)
        self.assertFalse(load_config(self.repo)["notify.telegram_enabled"])
        self.assertNotIn("SECRET-TOKEN", out.getvalue())
        self.assertIn("Unauthorized", out.getvalue())
        self.assertFalse(any("getUpdates" in p for p in self.server.paths))
        self.assertNotIn("sendMessage", self.server.methods())   # a failed getMe sends nothing

    def test_successful_check_enables_and_prints_username_not_token(self):
        """Mutation caught: not enabling; printing the token; wrong test text."""
        out = io.StringIO()
        ok = notify.check_and_enable(self.repo, self.toml(), self.env, out)
        self.assertTrue(ok)
        self.assertIn("@artel_test_bot", out.getvalue())
        self.assertNotIn("SECRET-TOKEN", out.getvalue())
        self.assertTrue(load_config(self.repo)["notify.telegram_enabled"])
        self.assertEqual(self.server.methods(), ["getMe", "sendMessage"])
        self.assertEqual(self.server.bodies[1]["chat_id"], CHAT)
        self.assertEqual(self.server.bodies[1]["text"], f"artel: notifier test from {self.repo_path.name}")
        self.assertIn(f"/bot{TOKEN}/", self.server.paths[0])

    def test_send_rejected_does_not_enable(self):
        """Mutation caught: enabling on getMe alone (the message must be accepted, spec §7)."""
        self.server.responses["sendMessage"] = (400, {"ok": False, "description": "chat not found"})
        out = io.StringIO()
        self.assertFalse(notify.check_and_enable(self.repo, self.toml(), self.env, out))
        self.assertFalse(load_config(self.repo)["notify.telegram_enabled"])
        self.assertIn("chat not found", out.getvalue())

    def test_enable_false_does_not_write_the_config(self):
        """Mutation caught: ignoring the enable flag."""
        out = io.StringIO()
        self.assertTrue(notify.check_and_enable(self.repo, self.toml(), self.env, out, enable=False))
        self.assertFalse(load_config(self.repo)["notify.telegram_enabled"])

    def test_notify_test_command(self):
        """Mutation caught: `notify test` not enabling in artel.toml."""
        self.toml()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            cwd = os.getcwd()
            os.chdir(self.repo_path)
            try:
                code = notify.run_command(mock.Mock())
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("telegram_enabled = true", (self.repo_path / "artel.toml").read_text(encoding="utf-8"))
        self.assertTrue(load_config(self.repo)["notify.telegram_enabled"])

    def test_notify_test_command_fails_on_a_broken_bot(self):
        """Mutation caught: exit 0 although the configured notifier failed."""
        self.toml()
        self.server.responses["getMe"] = (401, {"ok": False, "description": "Unauthorized"})
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
            cwd = os.getcwd()
            os.chdir(self.repo_path)
            try:
                self.assertEqual(notify.run_command(mock.Mock()), 1)
            finally:
                os.chdir(cwd)


class LeakTest(NotifyBase):
    def check(self, cfg, env):
        out = io.StringIO()
        ok = notify.check_and_enable(self.repo, cfg, env, out)   # must not raise
        self.assertFalse(ok)
        self.assertNotIn("SECRET-TOKEN", out.getvalue())
        return out.getvalue()

    def test_api_base_without_scheme_does_not_raise_or_leak(self):
        """Mutation caught: the Request built outside the try (ValueError 'unknown url type' quotes the token)."""
        (self.repo_path / "artel.toml").write_text('[notify]\ntelegram_api_base = "api.telegram.org"\n',
                                                   encoding="utf-8", newline="\n")
        self.check(load_config(self.repo, env={}), self.env)

    def test_token_with_a_newline_does_not_raise_or_leak(self):
        """Mutation caught: http.client.InvalidURL (not an OSError/ValueError) escaping with the URL."""
        tf = self.tmp / "token.txt"
        tf.write_text(TOKEN + "\nSECOND-LINE\n", encoding="utf-8")
        self.check(self.toml(telegram_token_file=str(tf)), {"ARTEL_TELEGRAM_CHAT": CHAT})

    def test_credentials_file_with_bom_and_crlf(self):
        """Mutation caught: utf-8 instead of utf-8-sig (a Notepad BOM becomes part of the token)."""
        tf, cf = self.tmp / "t.txt", self.tmp / "c.txt"
        tf.write_bytes(b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n")
        cf.write_bytes(b"\xef\xbb\xbf42\r\n")
        c = notify.find_telegram_credentials(self.toml(telegram_token_file=str(tf), telegram_chat_file=str(cf)), {})
        self.assertEqual((c.token, c.chat), (TOKEN, "42"))

    def test_relative_credentials_file_is_relative_to_the_project(self):
        """Mutation caught: resolving against the cwd (artel started from a subdirectory finds nothing)."""
        (self.repo_path / "secrets").mkdir()
        (self.repo_path / "secrets" / "token").write_text(TOKEN, encoding="utf-8")
        cfg = self.toml(telegram_token_file="secrets/token")
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            c = notify.find_telegram_credentials(cfg, {"ARTEL_TELEGRAM_CHAT": CHAT})
        finally:
            os.chdir(cwd)
        self.assertEqual(c.token, TOKEN)

    def test_mixed_sources_token_from_file_chat_from_env(self):
        """Mutation caught: both values forced through one source."""
        tf = self.tmp / "t.txt"
        tf.write_text("FILETOKEN", encoding="utf-8")
        c = notify.find_telegram_credentials(self.toml(telegram_token_file=str(tf)), {"ARTEL_TELEGRAM_CHAT": "5"})
        self.assertEqual((c.token, c.chat), ("FILETOKEN", "5"))

    def test_token_like_text_without_word_boundary_is_masked(self):
        """Mutation caught: a \\b anchor in the token pattern."""
        text = notify.format_event(Event("blocked", 1, "a" + TOKEN, "blocked", "q"))
        self.assertNotIn("SECRET-TOKEN", text)

    def test_traceback_in_any_event_text_is_cut(self):
        """Mutation caught: spec §7 'never the traceback' only enforced for `error` (a paused text carried one)."""
        e = Event("paused", None, None, None, "internal error: boom\nTraceback (most recent call last):\n  File x")
        self.assertEqual(notify.format_event(e), "artel: paused\ninternal error: boom")


class DispatcherTest(NotifyBase):
    def dispatcher(self, **values):
        values.setdefault("telegram_enabled", True)
        return notify.Dispatcher.from_config(self.toml(**values), self.env)

    def test_dispatcher_filters_by_on(self):
        """Mutation caught: sending every kind."""
        d = self.dispatcher()
        d.emit(Event("done", 3, "t", "done"))
        self.assertEqual(self.server.paths, [])
        d.emit(Event("blocked", 3, "t", "blocked", "which db?"))
        self.assertEqual(self.server.methods(), ["sendMessage"])

    def test_done_sent_immediately_when_selected(self):
        """Mutation caught: a digest or a drop of `done` when it is in `on`."""
        d = self.dispatcher(on=["done"])
        d.emit(Event("done", 3, "Add hello", "done"))
        self.assertEqual(self.server.bodies[0]["text"], "artel: #3 done — Add hello")

    def test_awaiting_accept_is_sent_by_default(self):
        """Mutation caught: a hard-coded kind list that lacks awaiting_accept."""
        d = self.dispatcher()
        d.emit(Event("awaiting_accept", 5, "Risky", "awaiting_accept", "merge=ask"))
        self.assertEqual(len(self.server.bodies), 1)
        self.assertIn("merge=ask", self.server.bodies[0]["text"])

    def test_every_event_kind_is_deliverable(self):
        """Mutation caught: a kind the formatter or filter cannot handle."""
        d = self.dispatcher(on=list(events.EVENT_KINDS))
        for kind in events.EVENT_KINDS:
            d.emit(Event(kind, 1, "t", "x", "text"))
        self.assertEqual(len(self.server.bodies), len(events.EVENT_KINDS))

    def test_notify_on_choices_equal_event_kinds(self):
        """Mutation caught: Tasks 15 and 30 drifting apart (a kind that can never be selected or never exists)."""
        self.assertEqual(set(config_schema.by_key("notify.on").choices), set(events.EVENT_KINDS))

    def test_paused_resets_at_formatted_local_and_utc(self):
        """Mutation caught: a raw epoch, only one zone, or the reset time dropped."""
        ts = 1_800_000_000.0
        text = notify.format_event(Event("paused", None, None, None, "limit: five-hour window (quota_stop_five_hour)",
                                         resets_at=ts))
        utc = datetime.fromtimestamp(ts, timezone.utc)
        local = utc.astimezone()
        self.assertIn(f"{utc:%Y-%m-%d %H:%M} UTC", text)
        self.assertIn(f"{local:%Y-%m-%d %H:%M}", text)
        self.assertIn(f"UTC{local:%z}", text)
        self.assertNotIn(str(int(ts)), text)
        self.assertIn("limit: five-hour window", text)
        self.assertNotIn("resets", notify.format_event(Event("paused", text="manual: by you")))

    def test_command_notifier_receives_json(self):
        """Mutation caught: text instead of JSON on stdin; a lossy encoding."""
        out = self.tmp / "event.json"
        cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w', encoding='utf-8').write(sys.stdin.read())",
               str(out)]
        notify.CommandNotifier(cmd, "auto").send_event(Event("blocked", 4, "Réseau ✓ тест", "blocked", "вопрос?"))
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((data["kind"], data["task_id"], data["title"], data["text"]),
                         ("blocked", 4, "Réseau ✓ тест", "вопрос?"))

    def test_command_notifier_stdin_is_ascii(self):
        """Mutation caught: raw UTF-8 on stdin (a child reading it with cp1252 on Windows fails on byte 0x81,
        e.g. in Cyrillic text)."""
        out = self.tmp / "raw.bin"
        cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())", str(out)]
        self.assertTrue(notify.CommandNotifier(cmd, "auto").send_event(Event("blocked", 4, "Réseau ✓ тест", "blocked")))
        raw = out.read_bytes()
        raw.decode("ascii")   # raises on any non-ASCII byte
        self.assertEqual(json.loads(raw)["title"], "Réseau ✓ тест")

    def test_command_notifier_gets_the_text_in_the_environment(self):
        """Mutation caught: ARTEL_NOTIFY_TEXT (promised by the notify.command explain) missing."""
        out = self.tmp / "env.txt"
        cmd = [sys.executable, "-c",
               "import os, sys; open(sys.argv[1], 'w', encoding='utf-8').write(os.environ['ARTEL_NOTIFY_TEXT'])",
               str(out)]
        self.assertTrue(notify.CommandNotifier(cmd, "auto").send_event(Event("stuck", 2, "t", "stuck", "why")))
        self.assertEqual(out.read_text(encoding="utf-8"), "artel: #2 stuck — t\nwhy")

    def test_notify_command_env_has_artel_child(self):
        """Mutation caught: the notify command run without ARTEL_CHILD=1 (a notify script could then answer,
        approve or accept in the human's name, PD-65)."""
        out = self.tmp / "child.txt"
        cmd = [sys.executable, "-c",
               "import os, sys; open(sys.argv[1], 'w', encoding='utf-8').write(os.environ.get('ARTEL_CHILD', '-'))",
               str(out)]
        with mock.patch.dict(os.environ):
            os.environ.pop("ARTEL_CHILD", None)   # the runner's own environment does not carry it
            self.assertTrue(notify.CommandNotifier(cmd, "auto", ["ARTEL_CHILD"]).send_event(Event("stuck", 1)))
        self.assertEqual(out.read_text(encoding="utf-8"), "1")

    def test_command_notifier_does_not_pass_the_bot_token(self):
        """Mutation caught: the whole environment (bot token included) handed to a user-supplied command."""
        out = self.tmp / "env-keys.json"
        cmd = [sys.executable, "-c",
               "import json, os, sys; json.dump(sorted(os.environ), open(sys.argv[1], 'w', encoding='utf-8'))",
               str(out)]
        env = {"ARTEL_TELEGRAM_TOKEN": TOKEN, "TELEGRAM_BOT_TOKEN": TOKEN, "MY_BOT": TOKEN, "KEEP_ME": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(notify.CommandNotifier(cmd, "auto", ["MY_BOT"]).send_event(Event("stuck", 1)))
        keys = {k.upper() for k in json.loads(out.read_text(encoding="utf-8"))}
        self.assertTrue({"KEEP_ME", "ARTEL_NOTIFY_TEXT"} <= keys)
        self.assertFalse({"ARTEL_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "MY_BOT"} & keys)

    def test_from_config_drops_the_configured_token_env_name(self):
        """Mutation caught: from_config not passing notify.telegram_token_env to the command notifier."""
        cfg = self.toml(telegram_token_env="MY_OWN_TOKEN", command=[sys.executable, "-c", "pass"])
        d = notify.Dispatcher.from_config(cfg, self.env)
        self.assertIn("MY_OWN_TOKEN", d.command.drop_env)

    def test_command_timeout_counts_as_failure(self):
        """Mutation caught: `and not timed_out` dropped (a killed command reported as sent)."""
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        with mock.patch.object(notify, "COMMAND_TIMEOUT", 0.5):
            self.assertFalse(notify.CommandNotifier(cmd, "auto").send_event(Event("stuck", 1)))

    def test_telegram_payload_is_exactly_chat_id_and_text(self):
        """Mutation caught: parse_mode (Markdown injection from titles) or other fields added to sendMessage."""
        self.assertTrue(notify.TelegramNotifier(
            notify.TelegramCredentials(TOKEN, CHAT, "test"), self.server.api_base).send("hello"))
        self.assertEqual(self.server.bodies[-1], {"chat_id": CHAT, "text": "hello"})

    def test_long_event_text_is_clipped_below_the_telegram_limit(self):
        """Mutation caught: TEXT_LIMIT raised above Telegram's 4096 characters."""
        text = notify.format_event(Event("blocked", 1, "t", "blocked", "x" * 9000))
        self.assertLessEqual(len(text), 4096)

    def test_command_notifier_failure_is_false_not_an_exception(self):
        """Mutation caught: a failing or missing command raising out of the runner."""
        self.assertFalse(notify.CommandNotifier([sys.executable, "-c", "raise SystemExit(3)"], "auto")
                         .send_event(Event("error", 1)))
        self.assertFalse(notify.CommandNotifier(["/nonexistent/artel-notify"], "auto").send_event(Event("error", 1)))

    def test_the_on_filter_applies_to_the_command_notifier_too(self):
        """Mutation caught: the filter guarding only the Telegram branch."""
        marker = self.tmp / "ran.txt"
        cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w', encoding='utf-8').write('x')", str(marker)]
        self.dispatcher(command=cmd, telegram_enabled=False).emit(Event("done", 1, "t", "done"))
        self.assertFalse(marker.exists())

    def test_dispatcher_runs_both_notifiers(self):
        """Mutation caught: the command notifier skipped when Telegram is on (or the reverse)."""
        marker = self.tmp / "ran.txt"
        cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w', encoding='utf-8').write('x')", str(marker)]
        d = self.dispatcher(command=cmd)
        d.emit(Event("stuck", 1, "t", "stuck"))
        self.assertTrue(marker.exists())
        self.assertEqual(len(self.server.bodies), 1)

    def test_telegram_only_when_enabled_and_credentialed(self):
        """Mutation caught: sending without telegram_enabled, or building a notifier without credentials."""
        d = notify.Dispatcher.from_config(self.toml(telegram_enabled=False), self.env)
        self.assertIsNone(d.telegram)
        d = notify.Dispatcher.from_config(self.toml(telegram_enabled=True), {})
        self.assertIsNone(d.telegram)
        self.assertIsNone(d.command)

    def test_emit_never_raises_on_http_error(self):
        """Mutation caught: a dead server raising out of emit; the token leaking to stderr."""
        d = self.dispatcher()
        self.server.stop()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.emit(Event("blocked", 1, "t", "blocked", "q"))
        self.assertIn("telegram notification failed", err.getvalue())
        self.assertNotIn("SECRET-TOKEN", err.getvalue())

    def test_emit_survives_an_unexpected_exception(self):
        """Mutation caught: no catch-all around the notifiers."""
        d = self.dispatcher()
        with mock.patch.object(notify, "format_event", side_effect=RuntimeError(f"boom {TOKEN}")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                d.emit(Event("blocked", 1, "t", "blocked", "q"))
        self.assertIn("notification error", err.getvalue())
        self.assertNotIn("SECRET-TOKEN", err.getvalue())

    def test_http_error_body_does_not_leak_the_token(self):
        """Mutation caught: echoing the server's description unscrubbed (a bot API may echo the URL)."""
        self.server.responses["sendMessage"] = (400, {"ok": False, "description": f"bad request /bot{TOKEN}/x"})
        d = self.dispatcher()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.emit(Event("blocked", 1, "t", "blocked", "q"))
        self.assertIn("bad request", err.getvalue())
        self.assertNotIn("SECRET-TOKEN", err.getvalue())


class FormatTest(unittest.TestCase):
    def test_format_event_has_no_secrets(self):
        """Mutation caught: a token-shaped string in an event text reaching the message."""
        text = notify.format_event(Event("blocked", 12, "Fix api", "blocked", f"use {TOKEN} please"))
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertTrue(text.startswith("artel: #12 blocked — Fix api\n"))

    def test_error_event_text_has_no_traceback(self):
        """Mutation caught: PD-38 — an error message carrying the event text (a traceback) or no `artel show`."""
        e = Event("error", 12, "Add hello", "writing",
                  "internal error in write; see artel show 12\nTraceback (most recent call last):\n  File x.py")
        text = notify.format_event(e)
        self.assertEqual(text, "artel: #12 internal error in write — Add hello\nsee artel show 12")
        self.assertNotIn("Traceback", text)

    def test_long_text_is_clipped(self):
        """Mutation caught: a message over Telegram's limit (sendMessage refuses it)."""
        text = notify.format_event(Event("blocked", 1, "t", "blocked", "x" * 10_000))
        self.assertLessEqual(len(text), notify.TEXT_LIMIT)

    def test_event_without_task(self):
        self.assertEqual(notify.format_event(Event("runner_stopped", text="stopped by you")),
                         "artel: runner stopped\nstopped by you")


class SourceTest(unittest.TestCase):
    def test_no_getupdates_in_source(self):
        """Mutation caught: any getUpdates code path (a string literal naming it) in production code."""
        for path in sorted((REPO_ROOT / "artel").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn("getupdates", node.value.lower(), f"{path.name}:{node.lineno}")

    def test_no_listening_sockets_in_production_code(self):
        """Acceptance: zero listening sockets in artel/."""
        for path in sorted((REPO_ROOT / "artel").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(source, r"\.(listen|serve_forever)\(", path.name)
            self.assertNotIn("HTTPServer", source, path.name)


if __name__ == "__main__":
    unittest.main()

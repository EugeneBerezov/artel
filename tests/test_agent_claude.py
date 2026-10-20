import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from artel import agent_claude as A, config as C, config_schema as S, platform as P
from artel.errors import ArtelError
from tests.helpers import subprocess_env

FIX = Path(__file__).parent / "fixtures" / "stream"
FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


def runtime_cfg(**overrides) -> dict:
    cfg = {s.key: C.coerce(s, s.default) for s in S.SETTINGS}
    cfg.update(overrides)
    return cfg


class ParserTest(unittest.TestCase):
    def feed_file(self, name):
        p = A.StreamParser()
        lines = (FIX / name).read_text(encoding="utf-8").splitlines()
        return p, [e for line in lines if (e := p.feed(line))]

    def test_structured_result_and_summed_usage(self):
        p, events = self.feed_file("result_structured.jsonl")
        self.assertFalse(p.failed())
        self.assertEqual(p.structured["outcome"], "done")
        self.assertTrue(p.usage.final)
        self.assertIsNotNone(p.usage.first_call)
        self.assertEqual([e.status for e in events], ["allowed"])
        # final usage is the SUM of the run (Q6), not the last call
        self.assertEqual((p.usage.input_tokens, p.usage.cache_creation_input_tokens), (9, 31684))
        self.assertEqual((p.usage.cost_usd, p.usage.num_turns, p.usage.duration_ms), (0.0066, 2, 4200))
        self.assertEqual(p.usage.first_call["cache_creation_input_tokens"], 27515)   # startup cost of call 1

    def test_stop_reason_tool_use_is_not_failure(self):
        p, _ = self.feed_file("result_structured.jsonl")
        record = json.loads((FIX / "result_structured.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["stop_reason"], "tool_use")
        self.assertFalse(p.failed())

    def test_error_result_is_failure(self):
        p, _ = self.feed_file("result_error.jsonl")
        self.assertTrue(p.failed())
        self.assertTrue(p.is_error)
        self.assertEqual(p.api_error_status, 500)
        self.assertFalse(p.limit_hit())

    def test_missing_structured_output_is_failure(self):
        p = A.StreamParser()
        p.feed(A.sample_result_line(None))
        self.assertTrue(p.result_seen)
        self.assertTrue(p.failed())
        q = A.StreamParser()
        q.feed(json.dumps({"type": "result", "is_error": False, "structured_output": "not an object"}))
        self.assertTrue(q.failed())

    def test_killed_midway_keeps_partial_usage(self):
        p, _ = self.feed_file("killed_midway.jsonl")
        self.assertTrue(p.failed())
        self.assertFalse(p.result_seen)
        self.assertFalse(p.usage.final)
        self.assertEqual((p.usage.input_tokens, p.usage.output_tokens), (9, 52))
        self.assertEqual((p.usage.cache_creation_input_tokens, p.usage.cache_read_input_tokens), (31684, 23389))
        self.assertEqual(p.session_id, "7a1c2e4f-0000-4000-8000-00000000abcd")

    def test_partial_usage_counts_each_message_id_once(self):
        # PD-23 catches: summing every assistant line of one API call
        p = A.StreamParser()
        p.feed(A.sample_assistant_line("a", {"input_tokens": 5, "output_tokens": 1}, message_id="m1"))
        p.feed(A.sample_assistant_line("b", {"input_tokens": 5, "output_tokens": 9}, message_id="m1"))
        p.feed(A.sample_assistant_line("c", {"input_tokens": 2, "output_tokens": 3}, message_id="m2"))
        self.assertEqual((p.usage.input_tokens, p.usage.output_tokens), (7, 12))   # m1 last (5, 9) + m2 (2, 3)
        self.assertEqual(p.usage.first_call["output_tokens"], 9)   # the call's last usage, not its first line

    def test_permission_denials_collected(self):
        p, _ = self.feed_file("permission_denied.jsonl")
        self.assertEqual(len(p.denials), 1)
        self.assertEqual(p.denials[0]["tool_name"], "Bash")
        self.assertIn("x.txt", p.denials[0]["tool_input"]["command"])
        self.assertFalse(p.failed())   # denials do not change the outcome (PD-24)

    def test_session_id_and_context_tokens(self):
        p, _ = self.feed_file("result_structured.jsonl")
        self.assertEqual(p.session_id, "7a1c2e4f-0000-4000-8000-00000000abcd")
        self.assertEqual(p.usage.context_tokens(), 3 + 4169 + 23389)   # input + cache tokens of the LAST call
        self.assertIsNone(A.StreamParser().usage.context_tokens())

    def test_rate_limit_fields_from_spike_line(self):
        _, events = self.feed_file("rate_limit_allowed.jsonl")
        ev = events[0]
        self.assertEqual((ev.status, ev.rank, ev.resets_at, ev.limit_type, ev.utilization),
                         ("allowed", 0, 1792464000, "five_hour", 0.1))
        self.assertIn("seven_day", ev.windows)
        self.assertEqual(ev.windows["seven_day"]["utilization"], 0.02)

    def test_unknown_status_ranks_as_warning(self):
        p = A.StreamParser()
        ev = p.feed(A.sample_rate_limit_line("something_new", 1792464000, "five_hour", 0.93))
        self.assertEqual((ev.rank, ev.limit_type, ev.utilization, ev.resets_at), (1, "five_hour", 0.93, 1792464000))
        self.assertEqual(p.max_rank, 1)
        self.assertFalse(p.limit_hit())

    def test_rejected_ranks_2(self):
        self.assertEqual([A.status_rank(s) for s in ("allowed", "rejected", "allowed_warning", "")], [0, 2, 1, 1])
        p = A.StreamParser()
        p.feed(A.sample_rate_limit_line("allowed", 1, "five_hour", 0.1))
        p.feed(A.sample_rate_limit_line("rejected", 2, "five_hour", 1.0))
        p.feed(A.sample_rate_limit_line("allowed", 3, "five_hour", 0.1))
        self.assertEqual(p.max_rank, 2)   # the highest rank of the run, not the last

    def test_limit_hit_from_error_result(self):
        # PD-40
        for status in (429, "429"):
            p = A.StreamParser()
            p.feed(A.sample_result_line(None, is_error=True, api_error_status=status))
            self.assertTrue(p.limit_hit(), status)
        p = A.StreamParser()
        p.feed(A.sample_result_line(None, is_error=True, api_error_status=500))
        self.assertFalse(p.limit_hit())
        p = A.StreamParser()
        p.feed(A.sample_result_line({"outcome": "ready", "summary": "x"}, api_error_status=429))   # not an error
        self.assertFalse(p.limit_hit())
        p = A.StreamParser()
        p.feed(A.sample_rate_limit_line("rejected", 1, "five_hour", 1.0))
        p.feed(A.sample_result_line({"outcome": "ready", "summary": "x"}))
        self.assertTrue(p.limit_hit())   # rank 2 even with a valid result
        self.assertFalse(p.failed())

    def test_bad_json_and_unknown_type_ignored(self):
        p = A.StreamParser()
        for line in ("", "not json", "[1, 2]", "null", '{"type": "brand_new"}', '{"type": "assistant"}',
                     '{"type": "assistant", "message": 5}', '{"type": "rate_limit_event"}',
                     '{"type": "result", "usage": [1]}', '{"type": "rate_limit_event", "rate_limit_info": 3}'):
            self.assertIsNone(p.feed(line), line)
        self.assertEqual(p.usage.input_tokens, 0)

    def test_samples_roundtrip_through_parser(self):
        p = A.StreamParser()
        p.feed(A.sample_init_line("sess-1"))
        p.feed(A.sample_assistant_line("hi", {"input_tokens": 4, "output_tokens": 2, "cache_creation_input_tokens": 1,
                                              "cache_read_input_tokens": 3}))
        ev = p.feed(A.sample_rate_limit_line("allowed", 1792464000, "seven_day", 0.5))
        self.assertEqual((ev.limit_type, ev.utilization), ("seven_day", 0.5))
        denial = {"tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": "x"}}
        p.feed(A.sample_result_line({"outcome": "pass", "summary": "ok"},
                                    {"input_tokens": 8, "output_tokens": 4}, denials=[denial]))
        self.assertEqual(p.structured, {"outcome": "pass", "summary": "ok"})
        self.assertEqual((p.usage.input_tokens, p.usage.output_tokens, p.usage.final), (8, 4, True))
        self.assertEqual(p.denials, [denial])
        self.assertEqual(p.session_id, A.StreamParser().session_id or p.session_id)
        self.assertFalse(p.failed())

    def test_sample_lines_match_fixture_shape(self):
        fixture = [json.loads(line) for line in (FIX / "result_structured.jsonl").read_text(encoding="utf-8").splitlines()]
        by_type = {r["type"]: r for r in fixture}

        def shape(value):
            if isinstance(value, dict):
                return {k: shape(v) for k, v in value.items()}
            return None

        sample_result = json.loads(A.sample_result_line({"outcome": "done", "summary": "hello"}, {"input_tokens": 1}))
        for key in ("type", "is_error", "structured_output", "result", "usage", "total_cost_usd", "num_turns",
                    "permission_denials", "session_id", "api_error_status", "stop_reason", "duration_ms"):
            self.assertIn(key, sample_result)
            self.assertIn(key, by_type["result"])
        sample_limit = json.loads(A.sample_rate_limit_line("allowed", 1792464000, "five_hour", 0.1))
        self.assertEqual(set(sample_limit["rate_limit_info"]) - {"unifiedWindows"},
                         set(by_type["rate_limit_event"]["rate_limit_info"]) - {"unifiedWindows"})
        self.assertEqual(shape(sample_limit["rate_limit_info"]["unifiedWindows"]["five_hour"]),
                         shape(by_type["rate_limit_event"]["rate_limit_info"]["unifiedWindows"]["five_hour"]))
        sample_assistant = json.loads(A.sample_assistant_line("t", {"input_tokens": 1}))
        self.assertEqual(set(sample_assistant["message"]) & {"id", "usage", "content"}, {"id", "usage", "content"})


class ValidateTest(unittest.TestCase):
    def test_valid_per_stage(self):
        for stage, outcomes in A.STAGE_OUTCOMES.items():
            for outcome in outcomes:
                raw = {"outcome": outcome, "summary": "reason"}
                if outcome == "ask":
                    raw["question"] = "which one?"
                if outcome == "findings":
                    raw["findings"] = [{"severity": "major", "text": "t", "basis": "a.py:1"}]
                with self.subTest(stage=stage, outcome=outcome):
                    result, reason = A.validate_result(stage, raw)
                    self.assertIsNone(reason)
                    self.assertEqual(result["outcome"], outcome)

    def test_writer_cannot_pass(self):
        for stage in ("write", "fix"):
            for outcome in ("pass", "findings", "done"):
                result, reason = A.validate_result(stage, {"outcome": outcome, "summary": "x"})
                self.assertIsNone(result)
                self.assertIn("not allowed for stage " + stage, reason)

    def test_reviewer_cannot_ready(self):
        for stage in ("review", "attack", "vet"):
            result, reason = A.validate_result(stage, {"outcome": "ready", "summary": "x"})
            self.assertIsNone(result)
            self.assertIn("ready", reason)

    def test_findings_needs_items(self):
        for stage in ("review", "attack"):
            self.assertIsNotNone(A.validate_result(stage, {"outcome": "findings", "summary": "x"})[1])
            self.assertIsNotNone(A.validate_result(stage, {"outcome": "findings", "summary": "x", "findings": []})[1])
        result, reason = A.validate_result("review", {"outcome": "findings", "summary": "x", "findings": [
            {"severity": "minor", "text": "nit"}]})
        self.assertIsNone(reason)
        self.assertEqual(result["findings"], [{"severity": "minor", "text": "nit", "basis": ""}])

    def test_ask_needs_question(self):
        for bad in ({}, {"question": ""}, {"question": "   "}):
            self.assertIsNotNone(A.validate_result("write", {"outcome": "ask", "summary": "x", **bad})[1], bad)
        self.assertIsNone(A.validate_result("write", {"outcome": "ask", "summary": "x", "question": "q?"})[1])

    def test_bad_severity(self):
        for finding in ({"severity": "critical", "text": "t"}, {"text": "t"}, {"severity": "major", "text": ""},
                        {"severity": "major", "text": "t", "basis": 5}, "string"):
            result, reason = A.validate_result("review", {"outcome": "findings", "summary": "x", "findings": [finding]})
            self.assertIsNone(result, finding)
            self.assertIsNotNone(reason)

    def test_closed_ids_normalized(self):
        result, reason = A.validate_result("review", {"outcome": "pass", "summary": "x",
                                                      "closed": ["#12", 13, "14", " # 15 "]})
        self.assertIsNone(reason)
        self.assertEqual(result["closed"], [12, 13, 14, 15])
        for bad in (["x"], [True], [1.5], ["#"], "12", [None]):
            self.assertIsNotNone(A.validate_result("review", {"outcome": "pass", "summary": "x", "closed": bad})[1], bad)

    def test_non_dict_invalid(self):
        for bad in (None, [], "text", 5):
            self.assertEqual(A.validate_result("write", bad)[0], None)
        self.assertIsNotNone(A.validate_result("nonsense", {"outcome": "ready"})[1])
        self.assertIsNotNone(A.validate_result("write", {"outcome": "ready", "summary": 5})[1])

    def test_vet_fields_validated(self):
        ok, reason = A.validate_result("vet", {"outcome": "pass", "summary": "fine", "risky": True,
                                               "risk_signals": ["deletes data"], "duplicate_of": 4})
        self.assertIsNone(reason)
        self.assertEqual((ok["risky"], ok["risk_signals"], ok["duplicate_of"]), (True, ["deletes data"], 4))
        signals_only, _ = A.validate_result("vet", {"outcome": "pass", "summary": "x", "risk_signals": ["quotas or money"]})
        self.assertTrue(signals_only["risky"])   # PD-27: any returned signal makes the task risky
        plain, _ = A.validate_result("vet", {"outcome": "pass", "summary": "x"})
        self.assertEqual((plain["risky"], plain["risk_signals"], plain["duplicate_of"]), (False, [], None))
        for bad in ({"risky": "yes"}, {"risk_signals": "deletes data"}, {"risk_signals": [1]},
                    {"duplicate_of": "4"}, {"duplicate_of": True}, {"duplicate_of": 1.5}):
            self.assertIsNotNone(A.validate_result("vet", {"outcome": "pass", "summary": "x", **bad})[1], bad)
        null_dup, _ = A.validate_result("vet", {"outcome": "pass", "summary": "x", "duplicate_of": None})
        self.assertIsNone(null_dup["duplicate_of"])
        # inadequate: the reason is the summary, no findings list needed
        inadequate, reason = A.validate_result("vet", {"outcome": "findings", "summary": "too vague"})
        self.assertIsNone(reason)
        self.assertEqual(inadequate["summary"], "too vague")
        self.assertIsNotNone(A.validate_result("vet", {"outcome": "findings", "summary": " "})[1])

    def test_vet_fields_rejected_on_other_stages_ignored(self):
        result, reason = A.validate_result("write", {"outcome": "ready", "summary": "x", "risky": "garbage",
                                                     "risk_signals": 5, "duplicate_of": "x"})
        self.assertIsNone(reason)
        for key in ("risky", "risk_signals", "duplicate_of"):
            self.assertNotIn(key, result)


class ArgvTest(unittest.TestCase):
    def build(self, profile="writer", model="sonnet", **overrides):
        cfg = runtime_cfg(agent_command=[sys.executable, "x.py"], **overrides)
        return A.build_argv(cfg, profile=profile, model=model, notice_path=Path("/n/notice.md"))

    def test_writer_argv_has_common_and_writer_flags(self):
        argv = self.build("writer", model="haiku")
        self.assertEqual(argv[0], P.which(sys.executable) or sys.executable)
        self.assertEqual(argv[1], "x.py")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        for flag in ("-p", "--output-format", "stream-json", "--verbose", "--json-schema", "--permission-prompts",
                     "--disable-slash-commands", "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections",
                     "--append-system-prompt-file", "--allowedTools"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], str(Path("/n/notice.md")))
        self.assertNotIn("--disallowedTools", argv)

    def test_reviewer_argv_is_read_only_profile(self):
        argv = self.build("reviewer")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Edit Write NotebookEdit")
        self.assertNotIn("acceptEdits", argv)

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            self.build("planner")

    def test_schema_is_inline_json(self):
        argv = self.build()
        token = argv[argv.index("--json-schema") + 1]
        self.assertEqual(json.loads(token), A.RESULT_SCHEMA)
        self.assertEqual(token, A.schema_json())
        self.assertNotIn("{schema}", " ".join(argv))

    def test_schema_json_is_stable(self):
        text = A.schema_json()
        self.assertEqual(text, A.schema_json())
        self.assertEqual(text, json.dumps(A.RESULT_SCHEMA, separators=(",", ":"), ensure_ascii=True))
        self.assertNotIn(" ", text.replace("Edit Write", ""))   # compact
        result = subprocess.run([sys.executable, "-c", "from artel import agent_claude as A; print(A.schema_json())"],
                                capture_output=True, encoding="utf-8", env=subprocess_env())
        self.assertEqual(result.stdout.strip(), text)   # identical in another process (cached prefix, Q6)

    def test_schema_covers_every_validated_field(self):
        props = A.RESULT_SCHEMA["properties"]
        for key in ("outcome", "summary", "question", "findings", "closed", "risky", "risk_signals", "duplicate_of"):
            self.assertIn(key, props)
        self.assertEqual(props["outcome"]["enum"], ["ready", "pass", "findings", "ask", "fail"])
        self.assertEqual(props["findings"]["items"]["properties"]["severity"]["enum"], list(A.SEVERITIES))

    def test_project_tools_expand_to_separate_args(self):
        argv = self.build("writer", **{"commands.project_tools": ["Bash(npm test *)", "Bash(make *)"]})
        i = argv.index("Bash(npm test *)")
        self.assertEqual(argv[i:i + 2], ["Bash(npm test *)", "Bash(make *)"])
        self.assertNotIn("{project_tools}", argv)
        argv = self.build("reviewer", **{"commands.project_read_tools": ["Bash(rg *)"]})
        self.assertIn("Bash(rg *)", argv)

    def test_empty_project_tools_dropped(self):
        argv = self.build("writer")
        self.assertNotIn("{project_tools}", argv)
        self.assertNotIn("", argv)
        self.assertEqual(argv[-2], "--allowedTools")   # the tool pattern string stays the last token

    def test_agent_command_list_and_string(self):
        cfg = runtime_cfg(agent_command=[sys.executable, "x.py"])
        self.assertEqual(A.agent_argv(cfg)[1:], ["x.py"])
        with mock.patch.object(P, "which", lambda name: "/opt/bin/" + name):
            self.assertEqual(A.agent_argv(runtime_cfg(agent_command="claude")), ["/opt/bin/claude"])
            # PD-44: a string is ONE executable, backslashes and spaces survive
            for path in ("C:\\Tools\\claude.exe", "/opt/my tools/claude"):
                self.assertEqual(A.agent_argv(runtime_cfg(agent_command=path)), ["/opt/bin/" + path])
        with mock.patch.object(P, "which", lambda name: None):
            with self.assertRaises(ArtelError):
                A.agent_argv(runtime_cfg(agent_command="claude"))

    def test_missing_permission_mode_refused(self):
        with self.assertRaises(ArtelError) as cm:
            self.build("writer", **{"profiles.writer": ["--allowedTools", "Bash(git add *)"]})
        self.assertIn("--permission-mode", str(cm.exception))

    def test_bypass_permissions_refused(self):
        for tokens in (["--permission-mode", "bypassPermissions"], ["--permission-mode=bypassPermissions"],
                       ["--permission-mode", "default", "--dangerously-skip-permissions"]):
            with self.assertRaises(ArtelError, msg=tokens):
                self.build("writer", **{"profiles.writer": tokens})
        with self.assertRaises(ArtelError):   # also when it arrives through profiles.common
            self.build("reviewer", **{"profiles.common": ["-p", "--permission-mode", "bypassPermissions"]})

    def test_prompt_never_in_argv(self):
        argv = self.build()
        self.assertEqual(argv.count("-p"), 1)
        self.assertNotIn("{prompt}", " ".join(argv))
        # no positional prompt: every non-flag token follows a flag that takes a value
        self.assertEqual(argv[argv.index("-p") + 1], "--model")

    def test_unwrap_npm_cmd_shim(self):
        newer = ('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n'
                 'CALL :find_dp0\r\n\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
                 '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
                 'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
                 '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n')
        older = ('@IF EXIST "%~dp0\\node.exe" (\r\n  "%~dp0\\node.exe"  '
                 '"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n) ELSE (\r\n  @SETLOCAL\r\n'
                 '  @SET PATHEXT=%PATHEXT:;.JS;=;%\r\n  node  "%~dp0\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n)\r\n')
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            shim = Path(tmp) / "claude.cmd"
            cli = str(Path(tmp, "node_modules", "@anthropic-ai", "claude-code", "cli.js"))
            for text in (newer, older):
                self.assertEqual(A.unwrap_cmd_shim(text, shim), ["node", cli])   # no node.exe next to the shim
            (Path(tmp) / "node.exe").write_bytes(b"")
            for text in (newer, older):
                self.assertEqual(A.unwrap_cmd_shim(text, shim), [str(Path(tmp, "node.exe")), cli])

    def test_unwrap_write_wrapper_text(self):
        argv = ["C:\\Python 3\\python.exe", "C:\\r\\tests\\fake_agent.py", "100%done"]
        text = P._cmd_script(" ".join(f'"{P._bat_text(a)}"' for a in argv) + " %*")
        self.assertEqual(A.unwrap_cmd_shim(text, Path("C:/w/fake.cmd")), argv)

    def test_unknown_batch_file_refused(self):
        self.assertIsNone(A.unwrap_cmd_shim("@echo off\r\necho hello\r\n", Path("x.cmd")))
        self.assertIsNone(A.unwrap_cmd_shim("", Path("x.cmd")))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            bat = Path(tmp) / "claude.bat"
            bat.write_text("@echo off\r\ncall other.exe %*\r\n", encoding="utf-8", newline="")
            with mock.patch.object(P, "which", lambda name: name), self.assertRaises(ArtelError) as cm:
                A.agent_argv(runtime_cfg(agent_command=[str(bat)]))
            self.assertIn("agent_command", str(cm.exception))
            self.assertIn("claude.exe", str(cm.exception))

    def test_agent_argv_unwraps_cmd_and_keeps_extra_args(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            shim = Path(tmp) / "Claude.CMD"   # the suffix test is case-insensitive
            shim.write_text('@echo off\r\n"C:\\Py\\python.exe" "C:\\a\\fake.py" %*\r\n', encoding="utf-8", newline="")
            with mock.patch.object(P, "which", lambda name: name):
                argv = A.agent_argv(runtime_cfg(agent_command=[str(shim), "--bare"]))
            self.assertEqual(argv, ["C:\\Py\\python.exe", "C:\\a\\fake.py", "--bare"])

    def test_inline_schema_never_passes_cmd(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            fake_dir = tmp_path / "fake"
            fake_dir.mkdir()
            wrapper = P.write_wrapper(tmp_path / "fake-claude", [sys.executable, str(FAKE_AGENT)])
            cfg = runtime_cfg(agent_command=wrapper)
            argv = A.build_argv(cfg, profile="writer", model="sonnet", notice_path=tmp_path / "notice.md")
            if P.IS_WINDOWS:
                self.assertEqual(Path(argv[0]).name.lower(), Path(sys.executable).name.lower())   # unwrapped
            prompt = "Привет, это промпт ✓\nsecond line"
            result = subprocess.run(argv, input=prompt, encoding="utf-8", errors="replace", capture_output=True,
                                    cwd=tmp, env=subprocess_env(ARTEL_FAKE_DIR=str(fake_dir)))
            self.assertEqual(result.returncode, 0, result.stderr)
            call = json.loads((fake_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
            recorded = call["argv"]
            self.assertEqual(recorded[recorded.index("--json-schema") + 1], A.schema_json())   # byte for byte
            self.assertEqual(call["prompt"], prompt)   # the prompt arrived on stdin
            self.assertNotIn(prompt, recorded)

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from artel import agent_claude as A
from tests.helpers import RepoTestCase, git, subprocess_env

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


class FakeAgentTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.fake_dir = self.tmp / "fake"
        self.fake_dir.mkdir()

    def scenario(self, data):
        (self.fake_dir / "scenario.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")

    def run_fake(self, *, task="1", stage="write", prompt="do it", args=(), cwd=None, extra_env=None):
        env = subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir), ARTEL_TASK=task, ARTEL_STAGE=stage, ARTEL_ROUND="1")
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(FAKE_AGENT), *args], input=prompt, encoding="utf-8",
                              errors="replace", capture_output=True, cwd=str(cwd or self.repo_path), env=env)

    def calls(self):
        text = (self.fake_dir / "calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def parse(self, stdout):
        parser = A.StreamParser()
        events = [e for line in stdout.splitlines() if (e := parser.feed(line))]
        return parser, events

    def test_fake_records_call_and_emits_result(self):
        self.scenario({"write": [{"result": {"outcome": "ready", "summary": "ok"}}]})
        res = self.run_fake(prompt="привет ✓", args=["-p", "--model", "haiku"])
        self.assertEqual(res.returncode, 0, res.stderr)
        parser, _ = self.parse(res.stdout)
        self.assertEqual(parser.structured, {"outcome": "ready", "summary": "ok"})
        self.assertFalse(parser.failed())
        (call,) = self.calls()
        self.assertEqual((call["stage"], call["task"], call["model"], call["prompt"]), ("write", "1", "haiku", "привет ✓"))
        self.assertEqual(call["argv"], ["-p", "--model", "haiku"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo_path.resolve())

    def test_fake_commits(self):
        self.scenario({"*": [{"write": ["src/new.txt", "hello\n"]}, {"commit": "add new"},
                             {"result": {"outcome": "ready", "summary": "x"}}]})
        before = git(self.repo_path, "rev-parse", "HEAD")
        res = self.run_fake()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotEqual(git(self.repo_path, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo_path, "log", "-1", "--format=%s"), "add new")
        self.assertEqual((self.repo_path / "src" / "new.txt").read_bytes(), b"hello\n")

    def test_fake_scenario_key_precedence_and_counts(self):
        def result(text):
            return [{"result": {"outcome": "ready", "summary": text}}]

        self.scenario({"7:write#2": result("exact second"), "7:write": result("task stage"),
                       "write": result("stage"), "*": result("star")})
        got = [self.parse(self.run_fake(task="7", stage="write").stdout)[0].structured["summary"] for _ in range(3)]
        self.assertEqual(got, ["task stage", "exact second", "task stage"])   # #2 is the second call only
        self.assertEqual(self.parse(self.run_fake(task="8", stage="write").stdout)[0].structured["summary"], "stage")
        self.assertEqual(self.parse(self.run_fake(task="8", stage="review").stdout)[0].structured["summary"], "star")
        counts = json.loads((self.fake_dir / "counts.json").read_text(encoding="utf-8"))
        self.assertEqual(counts, {"7:write": 3, "8:write": 1, "8:review": 1})

    def test_fake_rate_limit_and_sleep(self):
        self.scenario({"*": [{"rate_limit": ["allowed_warning", 1792464000, "five_hour", 0.93]},
                             {"assistant": {"input_tokens": 4, "output_tokens": 2}}, {"sleep": 0.2},
                             {"result": {"outcome": "pass", "summary": "x"}}]})
        res = self.run_fake(stage="review")
        parser, events = self.parse(res.stdout)
        self.assertEqual([(e.status, e.rank, e.utilization) for e in events], [("allowed_warning", 1, 0.93)])
        self.assertEqual(parser.usage.input_tokens, 4)   # the result carries the usage of the assistant actions
        self.assertEqual(parser.max_rank, 1)

    def test_fake_result_usage_sums_assistant_calls(self):
        self.scenario({"*": [{"assistant": {"input_tokens": 4, "output_tokens": 2}},
                             {"assistant": {"input_tokens": 6, "output_tokens": 3}},
                             {"result": {"outcome": "pass", "summary": "x"}}]})
        parser, _ = self.parse(self.run_fake(stage="review").stdout)
        self.assertEqual((parser.usage.input_tokens, parser.usage.output_tokens), (10, 5))   # a SUM, like Q6
        self.assertEqual(parser.usage.last_call["input_tokens"], 6)

    def test_fake_parallel_agents_keep_every_call(self):
        self.scenario({"*": [{"sleep": 0.1}, {"result": {"outcome": "pass", "summary": "x"}}]})
        procs = []
        env = subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir), ARTEL_STAGE="review")
        for task in ("1", "1", "1", "1", "1", "1"):   # the same key: the counter read-modify-write must not lose updates
            e = dict(env, ARTEL_TASK=task)
            procs.append(subprocess.Popen([sys.executable, str(FAKE_AGENT)], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, cwd=str(self.repo_path), env=e))
        for proc in procs:
            proc.communicate(b"p")
            self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(json.loads((self.fake_dir / "counts.json").read_text(encoding="utf-8")), {"1:review": 6})

    def test_fake_sleep_really_sleeps(self):
        import time
        self.scenario({"*": [{"sleep": 0.6}, {"result": {"outcome": "pass", "summary": "x"}}]})
        start = time.monotonic()
        self.run_fake()
        self.assertGreaterEqual(time.monotonic() - start, 0.6)

    def test_fake_error_and_denial(self):
        self.scenario({"1:write": [{"denied": ["Bash", {"command": "rm -rf x"}]}, {"result_error": True}],
                       "2:write": [{"result_error": 429}],
                       "3:write": [{"denied": ["Bash", {"command": "ls"}]},
                                   {"result": {"outcome": "ready", "summary": "x"}}]})
        parser, _ = self.parse(self.run_fake(task="1").stdout)
        self.assertTrue(parser.failed() and parser.is_error)
        self.assertEqual(parser.denials[0]["tool_input"], {"command": "rm -rf x"})
        self.assertFalse(parser.limit_hit())
        parser, _ = self.parse(self.run_fake(task="2").stdout)
        self.assertTrue(parser.limit_hit())   # PD-40 shape
        parser, _ = self.parse(self.run_fake(task="3").stdout)
        self.assertFalse(parser.failed())
        self.assertEqual(parser.denials[0]["tool_name"], "Bash")

    def test_fake_default_fail(self):
        res = self.run_fake(task="9", stage="write")   # no scenario.json at all
        parser, _ = self.parse(res.stdout)
        self.assertEqual(parser.structured, {"outcome": "fail", "summary": "no scenario"})
        self.scenario({"other": []})
        parser, _ = self.parse(self.run_fake(task="9", stage="write").stdout)
        self.assertEqual(parser.structured["summary"], "no scenario")

    def test_fake_raw_exit_and_no_result(self):
        self.scenario({"1:write": [{"result_raw": "this is not json"}, {"exit": 3}, {"result": {"outcome": "ready", "summary": "x"}}],
                       "2:write": [{"write": ["a.txt", "x"]}]})
        res = self.run_fake(task="1")
        self.assertEqual(res.returncode, 3)
        self.assertIn("this is not json", res.stdout)
        self.assertTrue(self.parse(res.stdout)[0].failed())
        res = self.run_fake(task="2")   # actions without a result: a run that never delivered one
        self.assertTrue(self.parse(res.stdout)[0].failed())
        self.assertEqual((self.repo_path / "a.txt").read_text(encoding="utf-8"), "x")

    def test_fake_note_runs_artel_note(self):
        # the PATH shim is not built here: the fake falls back to `python -m artel`, which must fail cleanly
        # outside an initialised repository, proving the action really invokes artel
        self.scenario({"*": [{"note": ["FACT", "hi", "basis"]}]})
        res = self.run_fake()
        self.assertNotEqual(res.returncode, 0)

    def test_fake_records_pid(self):
        self.scenario({"*": [{"result": {"outcome": "ready", "summary": "x"}}]})
        env = subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir), ARTEL_TASK="1", ARTEL_STAGE="write")
        proc = subprocess.Popen([sys.executable, str(FAKE_AGENT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(self.repo_path), env=env)
        proc.communicate(b"prompt")
        (call,) = self.calls()
        self.assertEqual(call["pid"], proc.pid)   # the agent's own pid, not the test's
        self.assertNotEqual(call["pid"], os.getpid())

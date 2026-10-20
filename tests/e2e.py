"""End-to-end harness: a real git repository, the real runner and the fake agent (spec §13)."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from artel import board as B, config as C, config_schema as S, platform, slots, transitions
from artel.gitutil import INTEGRATION, Repo
from artel.runner import Runner
from tests.helpers import RepoTestCase, git, subprocess_env

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class E2ECase(RepoTestCase):
    def setUp(self):
        super().setUp()
        git(self.repo_path, "branch", INTEGRATION, "main")
        self.repo = Repo.discover(self.repo_path)
        self.fake_dir = self.tmp / "fake"
        self.fake_dir.mkdir()
        self.clock = FakeClock()
        self.events = []
        self.runners = []
        self.started_pids = set()
        self.settings = {}
        self.write_toml({})
        self.board = B.open_board(self.repo.board_path)
        self.cfg = C.load_config(self.repo, env={})
        # the runner's environment: only Runner(env=...) carries ARTEL_FAKE_DIR, never os.environ
        self.env = subprocess_env(ARTEL_FAKE_DIR=str(self.fake_dir))

    def tearDown(self):
        failures = []
        for runner in self.runners:
            try:
                runner.shutdown()
            except Exception as exc:   # noqa: BLE001 - the processes are still checked below
                failures.append(repr(exc))
        pids = self.recorded_pids()
        self.board.close()
        super().tearDown()
        self.assertEqual(failures, [], "a runner failed to shut down")
        deadline = time.monotonic() + 10
        alive = list(pids)
        while alive and time.monotonic() < deadline:
            alive = [p for p in alive if platform.pid_alive(p)]
            if alive:
                time.sleep(0.05)
        self.assertEqual(alive, [], "agent or job processes are still alive")

    # configuration --------------------------------------------------------------------------------------------

    def write_toml(self, overrides: dict) -> None:
        settings = {"poll_interval": "100ms", "agent_command": [sys.executable, str(FAKE_AGENT)],
                    "commands.verify": [sys.executable, "-c", "pass"], "stage_timeout": "60s", "kill_grace": "2s"}
        self.settings.update(overrides)   # overrides accumulate: runner(max_agents=1) keeps an earlier write_toml
        settings.update(self.settings)
        lines = []
        tables: dict[str, list[str]] = {}
        for key, value in settings.items():
            table, _, name = key.rpartition(".")
            line = f"{name} = {S.toml_literal(value)}"
            (tables.setdefault(table, []) if table else lines).append(line)
        for table, rows in tables.items():
            lines += ["", f"[{table}]", *rows]
        (self.repo.root / "artel.toml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def runner(self, **config_overrides) -> Runner:
        self.write_toml(config_overrides)
        cfg = C.load_config(self.repo, env={})
        self.cfg = cfg
        runner = Runner(self.repo, cfg, env=self.env, clock=self.clock, notify=self.events.append)
        original = runner._on_start

        def recording(board, job):   # every process a job starts (agent, setup, verify, base) must be dead at the end
            inner = original(board, job)

            def on_start(kind, pid, group, fingerprint):
                self.started_pids.add(pid)
                inner(kind, pid, group, fingerprint)
            return on_start

        runner._on_start = recording
        self.runners.append(runner)
        return runner

    # helpers --------------------------------------------------------------------------------------------------

    def scenario(self, data: dict) -> None:
        (self.fake_dir / "scenario.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")

    def calls(self) -> list[dict]:
        path = self.fake_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def add_task(self, title: str, **flags) -> int:
        return transitions.create(self.board, "add_human", title=title, **flags)

    def commit_of(self, message: str) -> str:
        sha = git(self.repo_path, "log", "--all", "-1", "--format=%H", "--fixed-strings", f"--grep={message}")
        self.assertTrue(sha, f"no commit with message {message!r}")
        return sha

    def wait_until(self, predicate, timeout: float = 30.0, interval: float = 0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        self.fail("condition not reached in time\n" + self.describe_board())

    def describe_board(self) -> str:
        rows = [f"#{t.id} {t.state} attempts={t.attempts} round={t.round} running={t.running} fix_kind={t.fix_kind}"
                for t in self.board.list_tasks()]
        meta = {k: self.board.get_meta(k) for k in ("pause_reason", "pause_detail")}
        entries = self.board.conn.execute("SELECT kind, task_id, text FROM entries ORDER BY id DESC LIMIT 12").fetchall()
        return "\n".join(rows + [str(meta)] + [f"  {e[0]} #{e[1]}: {e[2]}" for e in reversed(entries)])

    def drive(self, runner: Runner, until, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            runner.tick()
            if until():
                return
            if time.monotonic() > deadline:
                self.fail("drive timed out\n" + self.describe_board())
            runner.wait(0.1)

    def recorded_pids(self) -> list[int]:
        pids = {c["pid"] for c in self.calls()} | self.started_pids
        for task in self.board.list_tasks():
            if task.running_pid:
                pids.add(task.running_pid)
        raw = self.board.get_meta("base_running")
        if raw:
            pids.add(json.loads(raw)["pid"])
        return sorted(pids)

    def assertSlotsCleanAndDetached(self):
        for slot in self.board.list_slots():
            path = Path(slot.path)
            self.assertEqual(git(path, "branch", "--show-current"), "", f"slot {slot.n} is on a branch")
            self.assertEqual(git(path, "status", "--porcelain"), "", f"slot {slot.n} is dirty")
            merge = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path, capture_output=True)
            self.assertNotEqual(merge.returncode, 0, f"slot {slot.n} is mid-merge")

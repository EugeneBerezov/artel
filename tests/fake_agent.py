"""A fake `claude -p` for tests: speaks stream-json through the adapter's sample_* helpers.

Used as agent_command = [sys.executable, "<repo>/tests/fake_agent.py"]. Reads the prompt on stdin,
ARTEL_TASK / ARTEL_STAGE / ARTEL_ROUND from the environment and $ARTEL_FAKE_DIR/scenario.json.
Scenario keys, first match wins: "<task>:<stage>#<n>", "<task>:<stage>", "<stage>", "*".
Actions: write, read, git, commit, note, assistant, rate_limit, sleep, result, result_error, denied, result_raw, exit,
crash. `{"crash": <exit code>, "stderr": <line>}` writes the line to stderr and exits with the code; as the first
action it comes before any stream record (an agent CLI that dies before its first model call, PD-57).
`{"result_error": <status>}` is an error result with that `api_error_status` (429, 5xx, 529; `true`: none).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artel import agent_claude as A   # noqa: E402

DEFAULT_ACTIONS = [{"result": {"outcome": "fail", "summary": "no scenario"}}]


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class FileLock:
    """Cross-process lock by exclusive file creation; the agents of one test run in parallel."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + 10
        while True:
            try:
                os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return self
            except (FileExistsError, PermissionError):   # PermissionError: a delete-pending file on Windows
                if time.monotonic() > deadline:
                    return self   # proceed unlocked rather than wedge a test; never delete a lock we do not hold
                time.sleep(0.01)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass


def record_call(fake_dir: Path, record: dict, key: str) -> int:
    """Append the call and return the 1-based count of `key`, both under one lock."""
    with FileLock(fake_dir / "fake.lock"):
        with open(fake_dir / "calls.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        path = fake_dir / "counts.json"
        counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        counts[key] = counts.get(key, 0) + 1
        path.write_text(json.dumps(counts), encoding="utf-8", newline="\n")
        return counts[key]


PROBE_ACTIONS = [{"assistant": {"input_tokens": 1, "output_tokens": 1}}, {"result": {"summary": "OK"}}]


def pick_actions(scenario: dict, task: str, stage: str, n: int) -> list:
    for key in (f"{task}:{stage}#{n}", f"{task}:{stage}", stage, "*"):
        if key in scenario:
            return scenario[key]
    return PROBE_ACTIONS if stage == "probe" else DEFAULT_ACTIONS


def run_git(*args: str) -> None:
    subprocess.run(["git", "-c", "commit.gpgsign=false", *args], check=True, capture_output=True,
                   encoding="utf-8", errors="replace")


def run_note(kind: str, text: str, basis: str) -> None:
    artel = shutil.which("artel")   # the PATH shim the runner puts first (spec §5); no silent fallback
    if artel is None:
        print("fake_agent: no `artel` shim on PATH", file=sys.stderr)
        raise SystemExit(2)
    subprocess.run([artel, "note", kind, text, "--basis", basis], check=True, capture_output=True,
                   encoding="utf-8", errors="replace")


def crash(action: dict) -> int:
    line = action.get("stderr")
    if line is not None:
        sys.stderr.write(str(line) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    return int(action["crash"])


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    prompt = sys.stdin.read()
    fake_dir = Path(os.environ["ARTEL_FAKE_DIR"])
    task = os.environ.get("ARTEL_TASK", "")
    stage = os.environ.get("ARTEL_STAGE", "") or "probe"   # no stage: the limit probe (PD-55), scenario key "probe"
    argv = sys.argv[1:]
    model = argv[argv.index("--model") + 1] if "--model" in argv and argv.index("--model") + 1 < len(argv) else None
    scenario_path = fake_dir / "scenario.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}
    n = record_call(fake_dir, {"argv": argv, "stage": stage, "task": task, "model": model, "prompt": prompt,
                               "cwd": os.getcwd(), "pid": os.getpid(),
                               "env": {k: v for k, v in os.environ.items() if k.startswith("ARTEL_")}},
                  f"{task}:{stage}")
    actions = pick_actions(scenario, task, stage, n)
    if actions and "crash" in actions[0]:
        return crash(actions[0])   # before any stream record
    emit(A.sample_init_line())
    usage = None
    denials: list[dict] = []
    for action in actions:
        if "crash" in action:
            return crash(action)
        (name, arg), = action.items()
        if name == "write":
            rel, content = arg
            target = Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        elif name == "read":   # copy a file of the cwd aside, to show a test what the agent saw
            rel, out_name = arg
            (fake_dir / out_name).write_text(Path(rel).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        elif name == "git":   # an arbitrary git command in the cwd (e.g. ["merge", "--abort"])
            run_git(*arg)
        elif name == "git":   # raw git in the slot (e.g. a fixer that resets away its work)
            run_git(*arg)
        elif name == "commit":
            run_git("add", "-A")
            run_git("commit", "-q", "-m", arg)
        elif name == "note":
            run_note(*arg)
        elif name == "assistant":
            usage = ({k: (usage or {}).get(k, 0) + arg.get(k, 0) for k in {*(usage or {}), *arg}}
                     if usage else dict(arg))   # the final usage sums calls (Q6), every key
            emit(A.sample_assistant_line("working", arg))
        elif name == "rate_limit":
            status, resets_at, limit_type, utilization = arg
            emit(A.sample_rate_limit_line(status, resets_at, limit_type, utilization))
        elif name == "sleep":
            time.sleep(arg)
        elif name == "result":
            emit(A.sample_result_line(arg, usage, denials=denials))
        elif name == "result_error":
            status = None if arg is True else arg
            emit(A.sample_result_line(None, usage, is_error=True, denials=denials, api_error_status=status))
        elif name == "denied":
            tool_name, tool_input = arg
            denials.append({"tool_name": tool_name, "tool_use_id": "toolu_fake", "tool_input": tool_input})
        elif name == "result_raw":
            emit(arg)
        elif name == "exit":
            sys.stdout.flush()
            return int(arg)
        else:
            raise SystemExit(f"fake_agent: unknown action {name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""A fake `claude -p` for tests: speaks stream-json through the adapter's sample_* helpers.

Used as agent_command = [sys.executable, "<repo>/tests/fake_agent.py"]. Reads the prompt on stdin,
ARTEL_TASK / ARTEL_STAGE / ARTEL_ROUND from the environment and $ARTEL_FAKE_DIR/scenario.json.
Scenario keys, first match wins: "<task>:<stage>#<n>", "<task>:<stage>", "<stage>", "*".
Actions: write, commit, note, assistant, rate_limit, sleep, result, result_error, denied, result_raw, exit.
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


def locked_update_counts(fake_dir: Path, key: str) -> int:
    """Increment and return the 1-based call count of `key`; agents may run in parallel."""
    lock = fake_dir / "counts.lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                break   # a stale lock must not wedge a test
            time.sleep(0.01)
    try:
        path = fake_dir / "counts.json"
        counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        counts[key] = counts.get(key, 0) + 1
        path.write_text(json.dumps(counts), encoding="utf-8", newline="\n")
        return counts[key]
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass


def pick_actions(scenario: dict, task: str, stage: str, n: int) -> list:
    for key in (f"{task}:{stage}#{n}", f"{task}:{stage}", stage, "*"):
        if key in scenario:
            return scenario[key]
    return DEFAULT_ACTIONS


def run_git(*args: str) -> None:
    subprocess.run(["git", "-c", "commit.gpgsign=false", *args], check=True, capture_output=True,
                   encoding="utf-8", errors="replace")


def run_note(kind: str, text: str, basis: str) -> None:
    artel = shutil.which("artel")
    argv = [artel] if artel else [sys.executable, "-m", "artel"]
    subprocess.run([*argv, "note", kind, text, "--basis", basis], check=True, capture_output=True,
                   encoding="utf-8", errors="replace")


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    prompt = sys.stdin.read()
    fake_dir = Path(os.environ["ARTEL_FAKE_DIR"])
    task = os.environ.get("ARTEL_TASK", "")
    stage = os.environ.get("ARTEL_STAGE", "")
    argv = sys.argv[1:]
    model = argv[argv.index("--model") + 1] if "--model" in argv and argv.index("--model") + 1 < len(argv) else None
    with open(fake_dir / "calls.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"argv": argv, "stage": stage, "task": task, "model": model, "prompt": prompt,
                             "cwd": os.getcwd(), "pid": os.getpid()}, ensure_ascii=False) + "\n")
    scenario_path = fake_dir / "scenario.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}
    n = locked_update_counts(fake_dir, f"{task}:{stage}")
    emit(A.sample_init_line())
    usage = None
    denials: list[dict] = []
    for action in pick_actions(scenario, task, stage, n):
        (name, arg), = action.items()
        if name == "write":
            rel, content = arg
            target = Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        elif name == "commit":
            run_git("add", "-A")
            run_git("commit", "-q", "-m", arg)
        elif name == "note":
            run_note(*arg)
        elif name == "assistant":
            usage = arg
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

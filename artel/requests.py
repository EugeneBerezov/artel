"""Human steering commands through the single writer (spec §3.1 "Single writer", §8; PD-19, PD-26, PD-30).

A human command only appends a request; `apply_pending`, called by the runner on its main thread at the start of
every tick, turns it into a transition. Nothing here but `apply_pending` changes task state.
"""
import contextlib
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from artel import journal, pauses, platform, slots, transitions
from artel.board import BoardBusy, Request, UnknownTask, open_board
from artel.errors import ArtelError, UsageError
from artel.gitutil import INTEGRATION, Repo, task_branch
from artel.transitions import Transition

COMMANDS = ("answer", "approve", "reject", "accept", "rework", "pause", "resume", "stop")
_DIFFSTAT_LINES = 20


def runner_running(repo, board) -> bool:
    """meta runner_pid alive and its fingerprint == meta runner_fp. Never touches the runner lock (a starting runner
    must not lose it to this check); with `board=None` and no board file, creates nothing."""
    if board is None:
        if not Path(repo.board_path).exists():
            return False
        board = open_board(repo.board_path)
        try:
            return runner_running(repo, board)
        finally:
            board.close()
    pid, fingerprint = board.get_meta("runner_pid"), board.get_meta("runner_fp")
    if not pid or fingerprint is None:
        return False
    try:
        pid_n = int(pid)
    except ValueError:
        return False
    if not platform.pid_alive(pid_n):
        return False
    return fingerprint == "" or platform.process_fingerprint(pid_n) == fingerprint   # "": the runner had none


def enqueue(repo, board, command: str, task_id: int | None, payload: dict) -> int:
    if command not in COMMANDS:
        raise ValueError(f"unknown request command: {command!r}")
    return board.add_request(command, task_id, payload)


# CLI handlers ---------------------------------------------------------------------------------------------------

def _refused(command: str) -> bool:
    if os.environ.get("ARTEL_CHILD"):   # PD-65 b: the variable alone decides
        print(f"artel {command} is a human command; refused inside a process started by artel (ARTEL_CHILD is set)",
              file=sys.stderr)
        return True
    return False


def _check_text(what: str, text: str, *, prefix: str = "", empty_ok: bool = False) -> None:
    if not empty_ok and not text.strip():
        raise UsageError(f"{what} must not be empty")
    size = journal.utf8_len(prefix + text)
    if size > journal.TEXT_LIMIT:
        raise UsageError(f"{what} makes a {size}-byte journal entry; at most {journal.TEXT_LIMIT}")


def _queue(command: str, task_id: int | None, payload: dict, *, evidence: bool = False) -> int:
    repo = Repo.discover(Path.cwd())
    if task_id is not None and not repo.board_path.exists():
        raise UsageError(f"no such task: #{task_id} (no board yet)")
    board = open_board(repo.board_path)
    try:
        if task_id is not None:
            try:
                board.get_task(task_id)
            except UnknownTask:
                raise UsageError(f"no such task: #{task_id}") from None
            if evidence:
                _print_evidence(repo, board, task_id)
        n = enqueue(repo, board, command, task_id, payload)
        print(f"request #{n} queued: {command}" + (f" #{task_id}" if task_id is not None else ""))
        print("the runner applies it on its next tick" if runner_running(repo, board)
              else "no runner is running; it applies when `artel run` starts")
    finally:
        board.close()
    return 0


def _print_evidence(repo, board, task_id: int) -> None:
    """What an accept or approve rests on (M10): the latest VERDICT, the diff stat, a flaky-verify FAIL."""
    entries = journal.task_entries(board, task_id)
    verdicts = [e for e in entries if e.kind == "VERDICT"]
    if verdicts:
        print(f"verdict: {verdicts[-1].text}")
    if slots.task_branch_exists(repo, task_id):
        stat = repo.git("diff", "--stat", f"{INTEGRATION}...{task_branch(task_id)}", check=False).stdout
        lines = stat.splitlines()
        if lines:
            print(f"diff {INTEGRATION}...{task_branch(task_id)}:")
            for line in lines[:_DIFFSTAT_LINES]:
                print(line)
            if len(lines) > _DIFFSTAT_LINES:
                print(f"... {len(lines) - _DIFFSTAT_LINES} more lines")
    fails = [e for e in entries if e.kind == "FAIL" and e.author == "runner"]
    if fails and fails[-1].text.startswith("flaky verify:"):
        print(fails[-1].text)


def run_answer(args) -> int:
    if _refused("answer"):
        return 2
    _check_text("the answer", args.text)
    return _queue("answer", args.id, {"text": args.text})


def run_approve(args) -> int:
    if _refused("approve"):
        return 2
    return _queue("approve", args.id, {}, evidence=True)


def run_reject(args) -> int:
    if _refused("reject"):
        return 2
    reason = args.reason or ""   # argparse leaves --reason None
    _check_text("the reason", reason, prefix="reject: ", empty_ok=True)
    return _queue("reject", args.id, {"reason": reason})


def run_accept(args) -> int:
    if _refused("accept"):
        return 2
    return _queue("accept", args.id, {}, evidence=True)


def run_rework(args) -> int:
    if _refused("rework"):
        return 2
    _check_text("the note", args.note, prefix="rework: ")
    return _queue("rework", args.id, {"note": args.note})


def run_pause(args) -> int:
    if _refused("pause"):
        return 2
    return _queue("pause", None, {})


def run_resume(args) -> int:
    if _refused("resume"):
        return 2
    return _queue("resume", None, {"force": bool(args.force)})


def run_stop(args) -> int:
    """PD-26 (amended): with a runner, a stop request plus the stop file; without one, nothing at all."""
    if _refused("stop"):
        return 2
    repo = Repo.discover(Path.cwd())
    if not runner_running(repo, None):
        print("no runner is running")
        return 0
    board = open_board(repo.board_path)
    try:
        n = enqueue(repo, board, "stop", None, {"now": bool(args.now)})
    finally:
        board.close()
    repo.stop_path.parent.mkdir(parents=True, exist_ok=True)
    repo.stop_path.write_text("now" if args.now else "soft", encoding="utf-8")
    print(f"request #{n} queued: stop" + (" --now" if args.now else ""))
    print("the runner applies it on its next tick")
    return 0


# applying (the runner's main thread) ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Applied:
    request: Request
    error: str | None
    transition: Transition | None


class RunnerHooks(Protocol):
    def terminate(self, task_id: int) -> None: ...      # stop the task's job and wait for it (PD-30)
    def stop(self, now: bool) -> None: ...
    def resumed(self, kinds: list[str]) -> None: ...    # notify `resumed` per removed kind


def _latest_ask(board, task_id: int) -> int | None:
    asks = [e for e in journal.task_entries(board, task_id) if e.kind == "ASK"]
    return asks[-1].id if asks else None


def _answer(board, task_id: int, text: str, *, ref: int | None = None) -> None:
    journal.append(board, "ANSWER", journal.truncate_utf8(text, journal.TEXT_LIMIT), author="human",
                   task_id=task_id, ref=ref)


def _git_effects(repo, board, cfg, task_id: int, effects) -> None:
    """After the request is applied: a failure here is journalled; the transition and the request stand."""
    try:
        slots.apply_git_effects(repo, board, cfg, task_id, effects)
    except Exception as exc:   # noqa: BLE001 - e.g. a human checked the branch out
        journal.append(board, "FAIL", journal.truncate_utf8(f"after {effects}: {exc}", journal.TEXT_LIMIT),
                       author="runner", task_id=task_id)


def _apply_one(repo, board, cfg, request: Request, hooks) -> Applied:
    """The state change and `finish_request` commit together: a request is applied exactly once."""
    command, task_id, payload = request.command, request.task_id, request.payload
    if command == "pause":
        with board.tx():
            pauses.add(board, "manual", "artel pause", now=time.time())
            board.finish_request(request.id)
        return Applied(request, None, None)
    if command == "resume":
        with board.tx():
            removed, error = pauses.resume(board, force=bool(payload.get("force")))
            board.finish_request(request.id, error=error)
        if removed:
            hooks.resumed(removed)
        return Applied(request, error, None)
    if command == "stop":
        hooks.stop(bool(payload.get("now")))
        with contextlib.suppress(OSError):   # Windows: `artel stop` may still hold it open; the next tick reads it
            repo.stop_path.unlink(missing_ok=True)
        board.finish_request(request.id)
        return Applied(request, None, None)
    if task_id is None:
        raise ArtelError(f"{command} needs a task")
    task = board.get_task(task_id)
    limits = transitions.Limits.from_cfg(task, cfg)
    tip = repo.rev(task_branch(task_id))
    if command == "reject":
        decided = transitions.decide(task, "reject", limits, tip=tip)   # a reject that is not allowed kills nothing
        if "terminate_running" in decided.effects:
            hooks.terminate(task_id)                                     # PD-30: the job first, then the transition
        task = board.get_task(task_id)
        limits = transitions.Limits.from_cfg(task, cfg)
    has_branch = slots.task_branch_exists(repo, task_id) if command == "rework" else None
    with board.tx():   # the transition, its ANSWER entry and the request together
        tr = transitions.apply(board, task_id, command, limits, has_branch=has_branch, tip=tip)
        if command == "answer":
            _answer(board, task_id, payload.get("text", ""), ref=_latest_ask(board, task_id))
        elif command == "reject":
            _answer(board, task_id, "reject: " + payload.get("reason", ""))           # PD-19
        elif command == "rework":
            _answer(board, task_id, "rework: " + payload.get("note", ""))             # PD-19
        elif command == "accept" and tr.from_state == "blocked":
            _answer(board, task_id, "accept: verify again as is")                     # PD-62, R35b
        board.finish_request(request.id)
    _git_effects(repo, board, cfg, task_id, tr.effects)
    return Applied(request, None, tr)


def apply_pending(repo, board, cfg, hooks: RunnerHooks) -> list[Applied]:
    """Applies every pending request in id order; a refused one is finished with its error text. A busy board
    leaves the rest pending for the next tick (a human's answer is never dropped for a lock)."""
    applied = []
    for request in board.pending_requests():
        try:
            applied.append(_apply_one(repo, board, cfg, request, hooks))
            continue
        except BoardBusy:
            break
        except (ArtelError, ValueError) as exc:   # TransitionError, UnknownTask, a job that would not stop, …
            error = str(exc) or type(exc).__name__
        except Exception:   # noqa: BLE001 - a broken request must not be retried on every tick
            error = journal.truncate_utf8("internal error: " + traceback.format_exc().strip().splitlines()[-1],
                                          journal.TEXT_LIMIT)
        board.finish_request(request.id, error=error)
        applied.append(Applied(request, error, None))
    return applied

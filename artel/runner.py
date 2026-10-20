"""The runner: one process, one lock, a tick loop on the main thread and one worker thread per job (spec §5).

Single writer (§3.1): workers run stages, verify and the base job and store their results; only the main thread
applies transitions. Limits, disk guard, signals, full reconcile and the notifier wiring belong to Task 22,
request handling to Task 18, vetting to Task 21.
"""
import json
import os
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from artel import agent_claude, cmd_agent, config as config_mod, integrate, journal, pick, platform, slots, stages
from artel import transitions
from artel.board import Board, open_board
from artel.errors import ArtelError
from artel.events import Event, Notify, null_notify
from artel.gitutil import INTEGRATION, Repo, task_branch

BLOCKING_STATES = ("blocked", "stuck")


@dataclass
class Job:
    kind: str                 # "stage" | "verify" | "base"
    task_id: int | None
    slot: int
    thread: threading.Thread
    stop: threading.Event
    result: object = None     # (StagePlan, StageRun) | VerifyResult | BaseUpdate
    error: str | None = None  # traceback tail if the job raised
    busy: Path | None = None  # SlotBusy.where if the branch turned out to be checked out elsewhere (PD-8)


def _tail(text: str, limit: int = 1200) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


class Runner:
    def __init__(self, repo: Repo, cfg, *, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time, notify: Notify = null_notify):
        self.repo = repo
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self.clock = clock
        self.notify = notify
        self.board: Board | None = None
        self.jobs: list[Job] = []
        self.rate_limits: list = []        # Task 22 turns these into pauses
        self.stopping = False
        self._lock = platform.FileLock(repo.lock_path)
        self._wake = threading.Event()
        self._closed = False
        self._last_tick_error: str | None = None
        self._integrate_skip: dict[int, float] = {}   # task id -> clock() until which integrate is not retried

    # lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if not self._lock.acquire():
            raise ArtelError(f"another artel runner holds {self.repo.lock_path}")
        try:
            self.board = open_board(self.repo.board_path)
            self.board.set_meta("runner_pid", str(os.getpid()))
            cmd_agent.ensure_shim(self.repo)
            try:
                agent_claude.agent_argv(self.cfg)
            except ArtelError as exc:
                raise ArtelError(f"agent_command {self.cfg['agent_command']!r} is not usable: {exc}") from exc
            integrate.ensure_integration_branch(self.repo, self.cfg["base"])
            slots.ensure_slots(self.repo, self.board, self.cfg, self.cfg["max_agents"])
            self.reconcile()
        except BaseException:
            self._release()
            raise

    def reconcile(self) -> None:
        """Core version: nothing survives a restart. Task 22 adds process-tree termination."""
        board = self.board
        for task in board.list_tasks():
            if task.running:
                board.clear_running(task.id)          # requeue: no counter
        board.del_meta("base_running")
        slots.reconcile_slots(self.repo, board, self.cfg)

    def _release(self) -> None:
        if self.board is not None:
            try:
                self.board.del_meta("runner_pid")
            except Exception:   # noqa: BLE001 - releasing the lock matters more
                pass
            self.board.close()
            self.board = None
        self._lock.release()

    def request_stop(self, now: bool) -> None:
        self.stopping = True
        if now:
            for job in self.jobs:
                job.stop.set()
        self._wake.set()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.request_stop(now=True)
        try:
            for job in list(self.jobs):
                job.thread.join()
            if self.board is not None:
                self._collect()
        finally:
            self._release()

    def wait(self, timeout: float) -> None:
        self._wake.wait(timeout)
        self._wake.clear()

    def run_forever(self) -> int:
        self.start()
        try:
            while True:
                self.tick()
                if self.stopping and not self.jobs:
                    break
                self.wait(self.cfg["poll_interval"])
        except KeyboardInterrupt:
            self.request_stop(now=True)
        finally:
            self.shutdown()
        return 0

    # workers ------------------------------------------------------------------------------------------------

    def _spawn(self, kind: str, task_id: int | None, slot: int, work: Callable[[Board, Job], object]) -> Job:
        stop = threading.Event()
        job = Job(kind=kind, task_id=task_id, slot=slot, thread=None, stop=stop)   # type: ignore[arg-type]

        def body() -> None:
            board = None
            try:
                board = open_board(self.repo.board_path)
                job.result = work(board, job)
            except slots.SlotBusy as exc:
                job.busy = exc.where
            except Exception:   # noqa: BLE001 - reported on the main thread as an internal error
                job.error = _tail(traceback.format_exc())
                self._leave_quietly(board, job.slot)   # a half-done job must not keep its branch checked out
            finally:
                if board is not None:
                    board.close()
                self._wake.set()

        job.thread = threading.Thread(target=body, name=f"artel-{kind}-{task_id}", daemon=True)
        self.jobs.append(job)
        job.thread.start()
        return job

    def _leave_quietly(self, board: Board | None, slot: int) -> None:
        if board is None:
            return
        try:
            slots.leave_slot(self.repo, board, self.cfg, slot)
        except Exception:   # noqa: BLE001 - the job's own error is the one to report
            pass

    def _on_start(self, board: Board, job: Job):
        def on_start(kind: str, pid: int, group: int, fingerprint: str | None) -> None:
            if job.kind == "base":   # PD-34: the base job belongs to no task
                board.set_meta("base_running", json.dumps({"pid": pid, "group": group, "fingerprint": fingerprint,
                                                           "slot": job.slot, "started_at": self.clock()}))
            else:
                board.set_running(job.task_id, kind=kind, pid=pid, group=group, fingerprint=fingerprint,
                                  slot=job.slot)
        return on_start

    def _stage_work(self, board: Board, job: Job):
        task = board.get_task(job.task_id)
        plan = stages.plan_stage(self.repo, board, self.cfg, task, job.slot, base_env=self.env)
        run = stages.run_stage(self.repo, board, self.cfg, plan, job.slot, on_start=self._on_start(board, job),
                               on_rate_limit=self.rate_limits.append, stop_event=job.stop)
        return plan, run

    def _verify_work(self, board: Board, job: Job):
        task = board.get_task(job.task_id)
        return integrate.verify_task(self.repo, board, self.cfg, task, job.slot, on_start=self._on_start(board, job),
                                     stop_event=job.stop, env=self.env)

    def _base_work(self, board: Board, job: Job):
        return integrate.base_update(self.repo, board, self.cfg, job.slot, on_start=self._on_start(board, job),
                                     stop_event=job.stop, env=self.env)

    # the tick -----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        self._collect()
        self._integrate_verified()
        if self.board.get_meta("pause_reason") or self.stopping:
            return
        if self._guarded("the base check", self._base_moved):
            return
        self._guarded("picking work", self._pick_and_start)

    def _guarded(self, what: str, fn):
        """An unexpected error in one step of the tick must not end the runner; it is recorded once per text."""
        try:
            return fn()
        except Exception:   # noqa: BLE001
            tail = traceback.format_exc()
            text = stages._clip_tail(f"internal error in {what}: ", tail)
            if self._last_tick_error != text:
                self._last_tick_error = text
                journal.append(self.board, "FAIL", text, author="runner")
            return True   # pick nothing this tick

    def _collect(self) -> None:
        for job in [j for j in self.jobs if not j.thread.is_alive()]:
            self.jobs.remove(job)
            try:
                self._finish(job)
            except Exception:   # noqa: BLE001 - one task's bookkeeping must not stop the runner
                self._finish_failed(job, traceback.format_exc())

    def _finish_failed(self, job: Job, tail: str) -> None:
        """The result of a job could not be applied. A task job is charged like any internal error, so the
        stage cannot run (and spend quota) forever; if even that fails the FAIL entry is all that is left."""
        board = self.board
        if job.kind == "base" or job.task_id is None:
            self._base_failed(tail)
            return
        try:
            task = board.get_task(job.task_id)
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            tr = stages.finish_internal_error(self.repo, board, self.cfg, job.task_id, tail)
            self.notify(Event("error", job.task_id, task.title, board.get_task(job.task_id).state,
                              f"internal error in {stage}; see artel show {job.task_id}"))
            self._after_transition(job.task_id, tr, "internal error")
        except Exception:   # noqa: BLE001
            self._record_finish_failure(job.task_id, tail)

    def _record_finish_failure(self, task_id: int | None, tail: str) -> None:
        board = self.board
        try:
            journal.append(board, "FAIL", stages._clip_tail("internal error while finishing a job: ", tail),
                           author="runner", task_id=task_id)
        finally:
            if task_id is not None:
                board.clear_running(task_id)

    def _finish(self, job: Job) -> None:
        board = self.board
        if job.kind == "base":
            board.del_meta("base_running")
            if job.error is not None:
                self._base_failed(job.error)
                return
            if job.result is None:
                return
            outcome = integrate.finish_base_update(self.repo, board, self.cfg, job.result)
            if outcome == "paused":
                self.notify(Event("paused", text=board.get_meta("pause_detail")))
            return
        if job.busy is not None:                      # PD-8: skipped, nothing applied, no attempt
            board.clear_running(job.task_id)
            return
        if job.error is not None:
            task = board.get_task(job.task_id)
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            tr = stages.finish_internal_error(self.repo, board, self.cfg, job.task_id, job.error)
            self.notify(Event("error", job.task_id, task.title, board.get_task(job.task_id).state,
                              f"internal error in {stage}; see artel show {job.task_id}"))   # PD-38: no traceback
            self._after_transition(job.task_id, tr, "internal error")
            return
        if job.kind == "stage":
            plan, run = job.result
            tr = stages.finish_stage(self.repo, board, self.cfg, job.task_id, plan, run)
            detail = f"setup {run.setup}"
        else:
            try:
                tr = integrate.finish_verify(self.repo, board, self.cfg, job.task_id, job.result)
            finally:
                board.clear_running(job.task_id)   # the job is over, whatever happened to the transition
            detail = job.result.detail
        self._after_transition(job.task_id, tr, detail)

    def _base_failed(self, error: str) -> None:
        board = self.board
        journal.append(board, "FAIL", stages._clip_tail("internal error in the base job: ", error), author="runner")
        detail = stages._clip_tail("internal error: ", error)
        board.set_meta("pause_reason", "base_conflict")                        # PD-34
        board.set_meta("pause_detail", detail)
        self.notify(Event("paused", text=detail))

    def _after_transition(self, task_id: int, tr, detail: str) -> None:
        if tr is None:
            return
        board = self.board
        task = board.get_task(task_id)
        if "pause_setup_failed" in tr.effects:
            text = journal.truncate_utf8(f"setup failed for task {task_id}: {detail}", journal.TEXT_LIMIT)
            board.set_meta("pause_reason", "setup_failed")
            board.set_meta("pause_detail", text)
            self.notify(Event("paused", task_id, task.title, task.state, text))
        if tr.to_state in BLOCKING_STATES:
            self.notify(Event(tr.to_state, task_id, task.title, tr.to_state, tr.cause or None))

    def _integrate_verified(self) -> None:
        board = self.board
        live = {j.task_id for j in self.jobs if j.task_id is not None}
        for task in board.list_tasks(["verified"]):
            if task.running or task.id in live or self._integrate_skip.get(task.id, 0) > self.clock():
                continue
            try:
                outcome = integrate.integrate_task(self.repo, board, self.cfg, task.id)
            except Exception:   # noqa: BLE001 - e.g. a human deleted the branch: record once, retry in a minute
                self._integrate_skip[task.id] = self.clock() + 60
                self._record_finish_failure(task.id, traceback.format_exc())
                continue
            if outcome == "done":
                self.notify(Event("done", task.id, task.title, "done"))

    def _free_slots(self) -> list[int]:
        used = {j.slot for j in self.jobs}
        return [n for n in range(1, self.cfg["max_agents"] + 1) if n not in used]

    def _base_moved(self) -> bool:
        """True while a base update is pending or running: nothing new is picked meanwhile (§5)."""
        if any(j.kind == "base" for j in self.jobs):
            return True
        if not integrate.base_needs_update(self.repo, self.cfg):
            return False
        if slots.checked_out_elsewhere(self.repo, INTEGRATION) is not None:
            return False   # PD-13: nobody moves a branch somebody has checked out; work goes on, the update waits
        free = self._free_slots()
        if free and not any(j.kind == "verify" for j in self.jobs):
            self._spawn("base", None, free[0], self._base_work)
        return True

    def _pick_and_start(self) -> None:
        board = self.board
        free = self._free_slots()
        if not free:
            return
        tasks = board.list_tasks()
        if "vet" not in stages.SUPPORTED_STAGES:   # they wait for Task 21; filtering after select would starve open tasks
            tasks = [t for t in tasks if t.state != "proposed"]
        busy = {j.task_id for j in self.jobs if j.task_id is not None}
        held = self._held_elsewhere(tasks)                       # PD-8: skipped before any transition, no slot wasted
        tasks = [t for t in tasks if t.id not in held]
        selected = pick.select(tasks, board.all_deps(), free_slots=len(free),
                               verify_busy=any(j.kind == "verify" for j in self.jobs), busy=busy)
        for task in selected:
            if not free:
                break
            task = self._start_transition(task)
            slot = free.pop(0)
            if task.state == "verifying":
                self._spawn("verify", task.id, slot, self._verify_work)
            else:
                self._spawn("stage", task.id, slot, self._stage_work)

    def _held_elsewhere(self, tasks) -> set[int]:
        """Tasks whose branch a human (any non-slot worktree) has checked out."""
        mine = {Path(s.path).resolve() for s in self.board.list_slots()}
        by_branch = {wt.branch: wt.path for wt in self.repo.worktrees()
                     if wt.branch and wt.path.resolve() not in mine}
        return {t.id for t in tasks if task_branch(t.id) in by_branch}

    def _start_transition(self, task):
        board = self.board
        if task.state in ("open", "proposed"):
            transitions.apply(board, task.id, "pick", transitions.Limits.from_cfg(task, self.cfg))
            task = board.get_task(task.id)
        if task.answered:
            board.update_task(task.id, expect_state=task.state, expect_version=task.version, answered=False)
            task = board.get_task(task.id)
        return task


def run_command(args) -> int:
    repo = Repo.discover(Path.cwd())
    cli = {"max_agents": (args.n, "flag -n")} if getattr(args, "n", None) else None
    cfg = config_mod.load_config(repo, cli=cli)
    return Runner(repo, cfg).run_forever()

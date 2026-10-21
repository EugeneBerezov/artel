"""The runner: one process, one lock, a tick loop on the main thread and one worker thread per job (spec §5).

Single writer (§3.1): workers run stages, verify and the base job and store their results; only the main thread
applies transitions, and human requests at the start of every tick (`requests.apply_pending`, Task 18). While
any pause is active the runner keeps ticking (§3.1 Pauses); notifications go out on one sender thread, never on
the tick thread. Usage events, quota windows, the night budget and the probes that end an open-ended pause are
here too (Task 35), and so are the guards of an unattended night: the `stage_runs` cap, the failure breaker,
the `agent_unavailable` pause and its escalation streak, and the run records of PD-71 (Task 37). Vetting belongs to
Task 21.
"""
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from artel import agent_claude, cmd_agent, config as config_mod, integrate, journal, notify as notify_mod, pauses
from artel import pick, platform, probe, procs
from artel import requests, slots, stages, transitions
from artel.board import Board, open_board
from artel.errors import ArtelError
from artel.events import Event, Notify, null_notify
from artel.gitutil import INTEGRATION, Repo, task_branch

BLOCKING_STATES = ("blocked", "stuck")
FINISHED_STATES = ("done", "rejected")   # their logs are pruned beyond log_keep_tasks
HEAVY_CHECK_TIMEOUT = 30.0               # heavy_lock_check reports a stale lock; it never waits on the lock
NOTIFY_FLUSH_MIN = 5.0                   # shutdown waits at least this long for queued events (runner_stopped)
_TIP_KEY = "integration_tip"             # PD-65 d: the sha the runner itself left artel/integration at
SAMPLE_GAP = 600.0                       # usage_samples: at most one per 10 minutes (PD-56)
SAMPLE_KEEP = 48 * 3600.0
BUDGET_WINDOW = 24 * 3600.0
LIMIT_WINDOW_MIN = 0.9                  # a window this full is the one a reset-less limit hit belongs to
LIMIT_DETAIL = "usage limit"             # one cause per kind: a probe that learns the reset time replaces it
LIMIT_KINDS = ("limit", "quota", "agent_unavailable")   # end at `until`, or by a probe when there is none
_QUOTA_KEYS = (("five_hour", "quota_stop_five_hour"), ("seven_day", "quota_stop_seven_day"))
BREAKER_MIN_TASKS = 2                    # PD-60 a: the failure breaker needs failures of this many distinct tasks
_FAILURE_STREAK = "failure_streak"       # PD-58: {"count": n, "tasks": [ids]}
_UNAVAILABLE_STREAK = "unavailable_streak"   # PD-58, PD-61: {"signature": str, "count": n}
_PROBE_KEY = "probe_running"             # like PD-34's base_running: the probe job's pid, group and fingerprint
PROBE_LOG_KEEP = 7 * 24 * 3600.0         # probe logs older than this are pruned (they belong to no task)


@dataclass
class Job:
    kind: str                 # "stage" | "verify" | "base" | "probe" (slot -1: it takes no slot)
    task_id: int | None
    slot: int
    thread: threading.Thread
    stop: threading.Event
    result: object = None     # (StagePlan, StageRun) | VerifyResult | BaseUpdate
    error: str | None = None  # traceback tail if the job raised
    busy: Path | None = None  # SlotBusy.where if the branch turned out to be checked out elsewhere (PD-8)
    version: int | None = None  # the task's version when the job was started: unchanged = nothing applied yet
    discarded: bool = False     # terminated for a request (PD-30): its result is never applied
    finished: bool = False      # the work returned: the thread is about to end, collect it now
    probe_kind: str | None = None   # the pause kind a probe job was started for
    started_at: float = 0.0         # the injected clock when it was spawned
    agent_started: bool = False     # a stage job's agent process was spawned (spec §3.1 Counters: stage_runs)


@dataclass(frozen=True)
class ReconcileReport:
    terminated: list[int]          # tasks whose recorded process tree was still alive and was terminated
    requeued: list[int]            # tasks whose `running` was cleared (state unchanged, no counter)
    heavy_lock_stale: str | None   # heavy_lock_check output when it reported a stale lock


def _tail(text: str, limit: int = 1200) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


class Runner:
    def __init__(self, repo: Repo, cfg, *, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time, notify: Notify = null_notify,
                 notify_kill: Callable[[], None] | None = None):
        """`notify_kill`: ends a notifier still busy when shutdown's flush deadline has passed (a hung notify
        command, notify.Dispatcher.kill)."""
        self.repo = repo
        self.cfg = cfg
        self.env = dict(os.environ if env is None else env)
        self.clock = clock
        self.notify = notify
        self.notify_kill = notify_kill
        self.board: Board | None = None
        self.jobs: list[Job] = []
        self.activity: dict[int, float] = {}           # PD-66: when the runner last started, ended or moved a task
        self.stage_activity: dict[int, float] = {}     # ... an agent stage, setup or verify job (0: the base job)
        self._rate_events: queue.SimpleQueue = queue.SimpleQueue()   # RateLimit events from worker threads
        self._setup_lifted_at: float = 0.0             # the last timed lift: only a stage started after it proves setup
        self._setup_outage: dict | None = None         # PD-31: the setup_failed outage a timed retry goes on with
        self.stopping = False
        self._lock = platform.FileLock(repo.lock_path)
        self._wake = threading.Event()
        self._closed = False
        self._started = False
        self._last_tick_error: str | None = None
        self._tick_failed = False
        self._hard_stop_at: float | None = None        # monotonic time of the first `stop --now`
        self._integrate_skip: dict[int, float] = {}   # task id -> clock() until which integrate is not retried
        self._held: dict[int, Path] = {}              # PD-8: held task id -> the worktree that has its branch
        self._setup_hashes: dict[int, str] = {}       # slot -> setup hash when setup failed there (PD-31)
        self._finished_count: int | None = None       # done + rejected tasks when logs were last pruned
        self._outbox: queue.Queue = queue.Queue()      # events for the sender thread
        self._sender: threading.Thread | None = None
        self._notify_errors: list[str] = []            # tracebacks of a raising notifier, journaled by the tick
        self.workers: int = cfg["max_agents"]          # PD-71 a: the effective worker count (`-n`, else max_agents)
        self._verify_due: dict[int, float] = {}        # PD-71 c: task id -> when it was first seen due for verify
        self._idle: dict[int, str] = {}                # PD-71 b: slot -> the reason of its open idle span
        self._pick_inputs: tuple | None = None         # (tasks, deps, busy) this tick gave pick.select
        self._unavailable_reset_at: float = float("-inf")   # PD-61: the streak's last reset by another outcome
        self._unavailable_counted_probe: float | None = None   # PD-61: the passing probe that counted the last run
        self._announced: set[str] | None = None        # while requests are applied: kinds already notified as paused

    # lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._closed:
            raise ArtelError("runner already stopped")
        if self._started:
            raise ArtelError("runner already started")
        if not self._lock.acquire():
            raise ArtelError(f"another artel runner holds {self.repo.lock_path}")
        try:
            self.board = open_board(self.repo.board_path)
            self._clear_stale_stop()   # before runner_running() can see this runner: a new `artel stop` must count
            self.board.set_meta("runner_fp", platform.process_fingerprint(os.getpid()) or "")
            self.board.set_meta("runner_pid", str(os.getpid()))
            pauses.migrate_legacy(self.board, now=self.clock())
            cmd_agent.ensure_shim(self.repo)
            try:
                agent_claude.agent_argv(self.cfg)
            except ArtelError as exc:
                raise ArtelError(f"agent_command {self.cfg['agent_command']!r} is not usable: {exc}") from exc
            integrate.ensure_integration_branch(self.repo, self.cfg["base"])
            self._adopt_integration_tip(visibly=True)
            slots.ensure_slots(self.repo, self.board, self.cfg, self.cfg["max_agents"])
            self._report_reconcile(self.reconcile())
            now = self.clock()
            for span in self.board.idle_spans():   # PD-71 b: left open by a crashed runner; its length is unknown
                if span.until is None:
                    self.board.close_idle(span.slot, span.since)
            self._verify_due = {t.id: now for t in self.board.list_tasks(["verifying"])}   # PD-71 c
            self._unavailable_reset_at = now   # PD-61: only a probe that passes from now on qualifies a crash
            # before the first pick; no window is fresh from the store
            self._guarded("the stored usage windows", lambda: self._evaluate_usage(reported=set()))
            if self.cfg["review_rounds"] == 0:   # PD-51
                print("warning: review_rounds = 0 behaves like 1: one review, blocking findings go to you"
                      " (no fix round)", file=sys.stderr)
            self.prune_logs()
            self._started = True
        except BaseException:
            self._release()
            raise

    def _report_reconcile(self, report: "ReconcileReport") -> None:
        """T22 controller review d: one FACT per stage a previous runner left behind, and its open `runs` rows
        closed (outcome `stopped`): nobody waits for a run that no process will finish."""
        board = self.board
        for task_id in report.requeued:
            task = board.get_task(task_id)
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            killed = task_id in report.terminated
            what = "terminated and requeued" if killed else "requeued (its process was gone)"
            journal.append(board, "FACT", f"reconcile: {stage} left running by a previous runner {what}"
                                          " without a counter", author="runner", task_id=task_id, stage=stage)
            for run in board.list_runs(task_id):
                if run.ended_at is None:
                    board.finish_run(run.id, outcome="stopped", usage={}, exit_code=None, timed_out=False,
                                     killed=killed)

    def _clear_stale_stop(self) -> None:
        """PD-26 amended: a stop file or stop request left from before must not stop this runner at once."""
        self.repo.stop_path.unlink(missing_ok=True)
        for request in self.board.pending_requests():
            if request.command == "stop":
                self.board.finish_request(request.id, error="stale: no runner")

    def child_env(self) -> dict[str, str]:
        """The environment of every process the runner starts (PD-65 a)."""
        return dict(self.env, ARTEL_CHILD="1")

    def reconcile(self) -> ReconcileReport:
        """Nothing of a previous runner survives (§5): its recorded process trees are terminated (never a reused
        pid), its tasks requeued without a counter, its base job forgotten, every slot detached."""
        board = self.board
        terminated, requeued = [], []
        for task in board.list_tasks():
            if not task.running:
                continue
            if task.running_pid and self._terminate_recorded(task.running_pid, task.running_group,
                                                             task.running_fingerprint):
                terminated.append(task.id)
            board.clear_running(task.id)          # requeue: no counter
            requeued.append(task.id)
        for key in ("base_running", _PROBE_KEY):   # PD-34; the probe job is recorded the same way (T35 SPEC-GAP)
            raw = board.get_meta(key)
            if raw:
                try:
                    rec = json.loads(raw)
                    self._terminate_recorded(int(rec["pid"]), rec.get("group"), rec.get("fingerprint"))
                except (ValueError, KeyError, TypeError):
                    pass   # unreadable: nothing can be signalled safely
            board.del_meta(key)
        slots.reconcile_slots(self.repo, board, self.cfg)
        return ReconcileReport(terminated, requeued, self._heavy_lock_check())

    def _terminate_recorded(self, pid: int, group: int | None, fingerprint: str | None) -> bool:
        """§3.1: a recorded pid is signalled only if its start time still matches. Without a recorded start time
        a live pid cannot be told from a reused one, so it is left alone; a dead leader's group is still swept."""
        if fingerprint is None and platform.pid_alive(pid):
            return False
        return platform.terminate_tree(pid, int(group or pid), self.cfg["kill_grace"], fingerprint)

    def _heavy_lock_check(self) -> str | None:
        """Runs commands.heavy_lock_check once: exit 1 = a stale lock, reported instead of waited on (§5)."""
        command = self.cfg["commands.heavy_lock_check"]
        stale = None
        if command:
            self.repo.tmp_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.repo.tmp_dir / "heavy-lock-check.log"
            log_path.unlink(missing_ok=True)   # run_tree appends
            try:
                result = procs.run_tree(procs.command_argv(command, self.cfg["shell"]), cwd=self.repo.root,
                                        env=self.child_env(), timeout=HEAVY_CHECK_TIMEOUT,
                                        kill_grace=self.cfg["kill_grace"], log_path=log_path, merge_stderr=True)
            except ArtelError as exc:
                print(f"warning: commands.heavy_lock_check could not run: {exc}", file=sys.stderr)
                result = None
            if result is not None and result.returncode == 1:
                try:
                    output = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    output = ""
                line = " ".join(output.split()) or "heavy_lock_check exit 1"
                stale = journal.truncate_utf8(line, journal.TEXT_LIMIT)
                print(f"warning: the heavy lock looks stale: {stale}", file=sys.stderr)
            elif result is not None and (result.timed_out or result.returncode != 0):
                print("warning: commands.heavy_lock_check failed"
                      f" ({'timed out' if result.timed_out else f'exit {result.returncode}'})", file=sys.stderr)
        if stale is None:
            self.board.del_meta("heavy_lock_stale")
        else:
            self.board.set_meta("heavy_lock_stale", stale)
        return stale

    def prune_logs(self) -> None:
        """Keeps the logs of the newest `log_keep_tasks` finished tasks; a task in flight keeps its logs."""
        finished = sorted(self.board.list_tasks(FINISHED_STATES), key=lambda t: (t.updated_at, t.id), reverse=True)
        self._finished_count = len(finished)
        for task in finished[self.cfg["log_keep_tasks"]:]:
            directory = self.repo.logs_dir / str(task.id)
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)   # Windows: a file still open stays; next prune
        self._prune_probe_logs()

    def _prune_probe_logs(self) -> None:
        """Probe logs belong to no task: they go by age, PROBE_LOG_KEEP after the probe started (the UTC stamp
        probe.py puts in the name, from the runner clock). A name without a stamp is not artel's and stays."""
        directory = self.repo.logs_dir / "probe"
        if not directory.is_dir():
            return
        cutoff = self.clock() - PROBE_LOG_KEEP
        for path in directory.iterdir():
            started = probe.log_time(path)
            if started is None or started >= cutoff:
                continue
            try:
                path.unlink()
            except OSError:
                pass   # Windows: a file still open stays; next prune

    def _release(self) -> None:
        if self.board is not None:
            try:
                self.board.del_meta("runner_pid")
                self.board.del_meta("runner_fp")
            except Exception:   # noqa: BLE001 - releasing the lock matters more
                pass
            self.board.close()
            self.board = None
        self._lock.release()

    def request_stop(self, now: bool) -> None:
        self.stopping = True
        if now:
            if self._hard_stop_at is None:
                self._hard_stop_at = time.monotonic()
            for job in self.jobs:
                job.stop.set()
        self._wake.set()

    def _stop_overdue(self) -> bool:
        """After `stop --now`, jobs that still run past kill_grace + JOIN_DEADLINE are left to shutdown's warning."""
        return self._hard_stop_at is not None and \
            time.monotonic() - self._hard_stop_at > self.cfg["kill_grace"] + procs.JOIN_DEADLINE

    # requests.RunnerHooks --------------------------------------------------------------------------------------

    def terminate(self, task_id: int) -> None:
        """Stops the task's job and waits for it; `running` is cleared without a counter (PD-30). A job that does
        not stop in time raises: the request fails and nothing is deleted under a live worker."""
        for job in self.jobs:
            if job.task_id == task_id and not job.discarded:
                job.stop.set()
                job.thread.join(self.cfg["kill_grace"] + procs.JOIN_DEADLINE)
                if job.thread.is_alive():
                    raise ArtelError(f"the job of task {task_id} did not stop; try again")
                job.discarded = True
        self.board.clear_running(task_id)

    def stop(self, now: bool) -> None:
        self.request_stop(now)

    def resumed(self, kinds: list[str]) -> None:
        if {"base_conflict", "integration_moved"} & set(kinds):
            self._adopt_integration_tip()   # PD-65 d: the human's repair of artel/integration is adopted
        if "setup_failed" in kinds:
            self._setup_outage = None
        for kind in kinds:
            if self._announced is not None:
                if kind not in self._announced:   # paused and resumed in one tick: never a lone `resumed`
                    detail = requests.MANUAL_DETAIL if kind == "manual" else "paused"
                    self._emit(Event("paused", text=f"{kind}: {detail}"))
                self._announced.discard(kind)
            self._emit(Event("resumed", text=kind))

    # pauses (§3.1 Pauses) -----------------------------------------------------------------------------------

    def pause(self, kind: str, detail: str, *, until: float | None = None, setting: str | None = None,
              window: str | None = None) -> None:
        """`window`: the usage window (five_hour / seven_day) the notification names (spec §5 step 5)."""
        if pauses.add(self.board, kind, detail, now=self.clock(), until=until, setting=setting):
            self._notify_paused(kind, window)

    def _notify_paused(self, kind: str, window: str | None = None) -> None:
        """The one place a `paused` event is built: kind, cause, the setting that caused it, the reset time."""
        pause = pauses.active(self.board).get(kind)
        if pause is None:
            return
        cause = pause.causes[-1]
        text = f"{kind}: {cause.detail}" + (f" [{window}]" if window else "") + (f" ({cause.setting})" if cause.setting else "")
        self._emit(Event("paused", text=text, resets_at=pause.until))

    def unpause(self, kind: str) -> None:
        if pauses.remove(self.board, kind):
            self._emit(Event("resumed", text=kind))

    def update_pauses(self) -> None:
        """Each kind ends on its own condition and never removes another kind. manual, base_conflict,
        integration_moved and failures end only through requests (`resume`)."""
        active = pauses.active(self.board)
        self._guarded("the disk check", lambda: self._update_disk("disk" in active))   # one failing check
        self._guarded("the setup pause", lambda: self._update_setup_failed(active.get("setup_failed")))
        self._guarded("the limit pauses", self._update_limit_pauses)

    def _update_setup_failed(self, setup) -> None:
        if setup is None:
            self._setup_hashes.clear()
        elif self._setup_inputs_changed():
            self.unpause("setup_failed")   # PD-31: the next stage runs setup again
            self._setup_hashes.clear()
            self._setup_outage = None
        elif setup.until is not None and self.clock() >= setup.until:
            # the timed retry: lifted without a `resumed`; if setup fails again with the same inputs it is the same
            # outage (one notification per outage), if it works `_setup_worked` announces the end
            if self._setup_outage is None:   # a restart lost the outage: the end is announced, as for any pause
                self.unpause("setup_failed")
            else:
                pauses.remove(self.board, "setup_failed")
                self._setup_lifted_at = self.clock()
            self._setup_hashes.clear()

    def _setup_worked(self, started_at: float) -> None:
        """A stage that got past setup (ran ok, or skipped with its inputs unchanged) ends the outage — only one that
        started after the timed lift: an older stage finishing proves nothing about the retry."""
        if (self._setup_outage is not None and started_at >= self._setup_lifted_at
                and "setup_failed" not in pauses.active(self.board)):
            self._setup_outage = None
            self._emit(Event("resumed", text="setup_failed"))

    def _update_disk(self, paused: bool) -> None:
        minimum = self.cfg["min_free_disk"]
        needed = minimum + minimum // 20 if paused else minimum   # 5 % margin: no pause/resume pair per tick
        slots_root = self.repo.root / self.cfg["slots_dir"]
        low = []
        for path in (self.repo.common_dir, slots_root if slots_root.is_dir() else self.repo.root):
            free = platform.free_bytes(path)
            if free < needed:
                low.append((path, free))
        if low and not paused:
            path, free = low[0]
            self.pause("disk", f"{_size(free)} free on {path}, below {_size(minimum)}", setting="min_free_disk")
        elif not low and paused:
            self.unpause("disk")

    def _setup_inputs_changed(self) -> bool:
        return any(slots.setup_changed(slots.slot_path(self.repo, self.cfg, n), self.cfg, stored)
                   for n, stored in self._setup_hashes.items())

    def _pause_setup_failed(self, task_id: int, detail: str, slot: int | None) -> None:
        if slot is not None:
            path = slots.slot_path(self.repo, self.cfg, slot)
            self._setup_hashes[slot] = slots.setup_hash(path, self.cfg["commands.setup"],
                                                        self.cfg["commands.setup_inputs"])
        text = journal.truncate_utf8(f"setup failed for task {task_id}: {detail}", journal.TEXT_LIMIT)
        until = self.clock() + self.cfg["setup_retry"]
        first_line = detail.strip().splitlines()[0] if detail.strip() else ""
        digest = self._setup_hashes.get(slot) if slot is not None else None
        outage = self._setup_outage
        if outage is not None and digest is not None and outage["hash"] == digest:
            # the retry failed again with the same inputs: the same outage, its original `since`
            pauses.add(self.board, "setup_failed", text, now=self.clock(), until=until, setting="commands.setup",
                       since=outage["since"])
            journal.append(self.board, "FAIL", journal.truncate_utf8(f"setup retry failed again: {first_line}",
                                                                      journal.TEXT_LIMIT), author="runner")
            if first_line != outage["line"]:   # a different error is news
                outage["line"] = first_line
                self._notify_paused("setup_failed")
            return
        self.pause("setup_failed", text, until=until, setting="commands.setup")
        self._setup_outage = {"hash": digest, "since": self.clock(), "line": first_line}

    # usage events, quota and limit pauses (spec §5 step 5, PD-22, PD-40, PD-56) -----------------------------------

    def on_rate_limit(self, ev) -> None:
        """Called from a worker thread; the main thread applies it at the next tick."""
        self._rate_events.put(ev)
        self._wake.set()

    def _apply_rate_events(self) -> None:
        events = []
        while True:
            try:
                events.append(self._rate_events.get_nowait())
            except queue.Empty:
                break
        for ev in events:   # one bad event (or a stored record of the wrong shape) must not drop the others
            self._guarded("a rate-limit event", lambda ev=ev: self._usage_event(ev))
        if events:
            reported = {name for ev in events for name in probe.event_windows(ev)}
            self._guarded("the usage windows", lambda: self._evaluate_usage(reported=reported))

    def _json_meta(self, key: str, default):
        raw = self.board.get_meta(key)
        try:
            value = json.loads(raw) if raw else default
        except ValueError:
            return default   # unreadable: treated as never stored, rewritten by the next event
        if not isinstance(value, type(default)):
            return default   # the wrong shape is as good as unreadable
        if isinstance(value, list):
            value = [s for s in value if isinstance(s, list) and len(s) == 2 and all(isinstance(x, (int, float)) for x in s)]
        return value

    def _usage_event(self, ev) -> None:
        now = self.clock()
        if ev.limit_type:
            self.board.set_meta(f"limit:{ev.limit_type}", json.dumps(
                {"status": ev.status, "rank": ev.rank, "resets_at": ev.resets_at, "utilization": ev.utilization,
                 "seen_at": now}))
        self._store_windows(probe.event_windows(ev), now)
        if ev.rank >= agent_claude.status_rank(self.cfg["limit_stop"]):
            if ev.resets_at is not None and ev.resets_at > now:
                until = ev.resets_at
            else:
                known = pauses.active(self.board).get("limit")
                if known is not None and known.until is not None and known.until > now:
                    return   # a later event without a reset must not replace a known end (T35 review M3a)
                until = self._window_at_its_limit(now)
            self._set_limit(until, ev.limit_type)

    def _set_limit(self, until: float | None, window: str | None) -> None:
        """The `limit` pause; while it holds its end only grows (a later report of a shorter window must not shorten
        it, T35 controller review 2)."""
        known = pauses.active(self.board).get("limit")
        if known is not None and known.until is not None and until is not None and until < known.until:
            return
        self.pause("limit", LIMIT_DETAIL, until=until, setting="limit_stop", window=window)

    def _window_at_its_limit(self, now: float) -> float | None:
        """A limit hit without a reset time (owner 2026-10-21, T35 M3b): the recorded window with the highest last
        utilization; at 0.9 or more with a future reset it is the window that ran out, and its reset ends the limit.
        Otherwise None: the probe schedule decides."""
        windows = list(self._usage_windows().values())
        if not windows:
            return None
        top = max(windows, key=lambda w: w["utilization"])
        reset = top.get("resets_at")
        if top["utilization"] >= LIMIT_WINDOW_MIN and isinstance(reset, (int, float)) and reset > now:
            return float(reset)
        return None

    def _usage_windows(self) -> dict:
        """Meta `usage_windows` with every window of the wrong inner shape dropped (T35-2 m-c): the record is
        rewritten without it and one FAIL names it, so a bad window can never stop the limit pauses."""
        stored = self._json_meta("usage_windows", {})
        bad = [name for name, w in stored.items()
               if not isinstance(w, dict) or not _is_number(w.get("utilization"))
               or not (w.get("resets_at") is None or _is_number(w.get("resets_at")))]
        if bad:
            for name in bad:
                del stored[name]
            self.board.set_meta("usage_windows", json.dumps(stored))
            journal.append(self.board, "FAIL", stages._clip(
                "usage_windows: ignored a malformed window record: " + ", ".join(map(str, bad))), author="runner")
        return stored

    def _store_windows(self, windows: dict, now: float) -> None:
        """PD-56: the latest event per window; the weekly utilization is also sampled for the night budget."""
        if not windows:
            return
        stored = self._usage_windows()
        samples = [s for s in self._json_meta("usage_samples", []) if s[0] > now - SAMPLE_KEEP]
        rolled = stored.get("seven_day", {}).get("resets_at")
        if isinstance(rolled, (int, float)) and rolled <= now:   # the weekly window rolled: older samples are another week
            samples = [s for s in samples if s[0] >= rolled]
        for name, window in windows.items():
            stored[name] = {"utilization": window["utilization"], "resets_at": window["resets_at"], "seen": now}
        week = windows.get("seven_day")
        if week is not None and samples and week["utilization"] < samples[-1][1] - 1e-9:
            samples = []   # the weekly window fell: it rolled (even with no reset time known), a new baseline
        if week is not None and (not samples or now - samples[-1][0] >= SAMPLE_GAP):
            samples.append([now, week["utilization"]])
        self.board.set_meta("usage_windows", json.dumps(stored))
        self.board.set_meta("usage_samples", json.dumps(samples))

    def _evaluate_usage(self, *, reported: set[str]) -> None:
        """Quota thresholds per window and the night budget over the stored samples. A window in `reported` was
        just reported by an event or a probe: with no future reset time it then pauses (and is probed); any other
        stored window pauses only with a future reset, so a stale record (one whose reset has passed, T35-2 m-b)
        cannot pause again on a result that did not report it."""
        now = self.clock()
        windows = self._usage_windows()
        for name, key in _QUOTA_KEYS:
            window = windows.get(name)
            if window is None:
                continue
            fresh = name in reported
            detail = f"{name} window at or above {key}"
            if window["utilization"] < self.cfg[key]:
                if fresh and pauses.drop_cause(self.board, "quota", detail) == "emptied":   # it no longer holds
                    self._emit(Event("resumed", text="quota"))
                continue
            reset = window.get("resets_at")
            if reset is not None and reset <= now:
                if fresh:
                    reset = None   # a fresh report without a future reset ends by the probe rule (spec §5 step 5)
                else:
                    continue
            elif reset is None and not fresh:
                continue
            self.pause("quota", detail, until=reset, setting=key, window=name)
        recent = [s for s in self._json_meta("usage_samples", []) if s[0] > now - BUDGET_WINDOW]
        budget = self.cfg["night_budget_seven_day"]
        if len(recent) >= 2 and recent[-1][1] - recent[0][1] >= budget - 1e-9:   # 0.35 - 0.20 must reach 0.15
            self.pause("quota", f"weekly window grew by {budget:g} within 24 hours", setting="night_budget_seven_day",
                       until=recent[0][0] + BUDGET_WINDOW, window="seven_day")

    def _update_limit_pauses(self) -> None:
        """limit, quota and agent_unavailable end at their `until`; with none they are probed (PD-55), at most one
        probe at a time and `limit_probes` in 24 h across all kinds, then they wait for `resume`."""
        now = self.clock()
        active = pauses.active(self.board)
        for kind in LIMIT_KINDS:
            pause = active.get(kind)
            if pause is None:
                continue
            if pause.until is not None:
                if now >= pause.until:
                    self.unpause(kind)
                continue
            if pause.exhausted:
                continue
            limit = active.get("limit")
            if kind != "limit" and limit is not None and limit.until is not None and limit.until > now:
                continue   # a known limit end is coming: probing now would only use up the probes (SPEC-GAP T35)
            times = pauses.probe_times(self.board, now=now)
            if len(times) >= self.cfg["limit_probes"]:
                pauses.set_exhausted(self.board, kind)
                self._emit(Event("paused", text=f"{kind}: {len(times)} probes in 24 h did not lift it; "
                                                f"`artel resume` is needed"))
            elif not self.stopping and not any(j.kind == "probe" for j in self.jobs) and probe.due(pause, self.cfg, now=now, times=times):
                self._spawn("probe", None, -1, self._probe_work).probe_kind = kind

    def _probe_work(self, board: Board, job: Job):
        on_start = self._on_start(board, job)
        return probe.run_probe(self.repo, self.cfg, self.child_env(), now=self.clock(), stop_event=job.stop,
                               on_start=lambda pid, group, fp: on_start("probe", pid, group, fp))

    def _finish_probe(self, job: Job) -> None:
        """Applies a probe's result to the pause it was started for (PD-55). Never touches a task."""
        board, now, kind = self.board, self.clock(), job.probe_kind
        result = job.result
        if job.error is not None or result is None:
            journal.append(board, "FAIL", stages._clip_tail("internal error in the probe: ", job.error or "no result"),
                           author="runner")
            pauses.record_probe(board, now=now, kind=kind, outcome="unavailable")
            return
        if result.error is not None:   # the agent cannot start: a failed probe (owner 2026-10-21)
            journal.append(board, "FAIL", stages._clip(f"probe: the agent cannot start: {result.error}"),
                           author="runner")
        pauses.record_probe(board, now=now, kind=kind, outcome=result.outcome)
        self._store_windows(result.windows, now)
        self._evaluate_usage(reported=set(result.windows))
        pause = pauses.active(board).get(kind)
        if pause is None:
            return                                         # a `resume` already ended it
        if probe.ended(pause, result, self.cfg, now=now):
            if kind == "quota":
                self._end_quota_windows(pause)
            else:
                self.unpause(kind)
        elif result.outcome == "limit" and result.resets_at is not None and result.resets_at > now:
            # a probed `limit` gets its end (same cause, replaced, no second notification); for another probed
            # kind the pause stays and the limit is its own kind
            self._set_limit(result.resets_at, None)

    def _end_quota_windows(self, pause) -> None:
        """A probe ends only the window causes of `quota` (T35-2 m-a): the night budget ends at its own end, 24 h
        after the baseline sample."""
        for cause in pause.causes:
            if cause.setting == "night_budget_seven_day":
                continue
            if pauses.drop_cause(self.board, "quota", cause.detail) == "emptied":
                self._emit(Event("resumed", text="quota"))

    # the integration tip (PD-65 d) ----------------------------------------------------------------------------

    def _adopt_integration_tip(self, visibly: bool = False) -> None:
        """`visibly` (at start, T22 controller review c): a ref moved while no runner ran is adopted — a human repair
        is normal — but journaled and announced, so a foreign move is never taken over unseen."""
        tip = self.repo.rev(INTEGRATION)
        if tip is None:
            return
        left = self.board.get_meta(_TIP_KEY)
        if visibly and left and left != tip:
            text = f"integration moved while stopped: {left}..{tip} adopted"
            journal.append(self.board, "FACT", text, author="runner")
            self._emit(Event("error", text=text))
        self.board.set_meta(_TIP_KEY, tip)

    def _keep_tip_if_moved_to(self, sha: str | None) -> None:
        if sha is not None and self.repo.rev(INTEGRATION) == sha:
            self.board.set_meta(_TIP_KEY, sha)

    def _integration_moved(self) -> bool:
        """True when artel/integration is not where the runner left it: no move of the ref may follow. Pauses
        `integration_moved` unless `base_conflict` is active (the human is repairing the branch then)."""
        left, now = self.board.get_meta(_TIP_KEY), self.repo.rev(INTEGRATION)
        if left is None or now is None or left == now:
            return False
        if "base_conflict" not in pauses.active(self.board):
            self.pause("integration_moved",
                       f"{INTEGRATION} at {now[:12]}, runner left it at {left[:12]}; resume adopts {now[:12]}")
        return True

    # lifecycle, continued ---------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.request_stop(now=True)
        try:
            grace = self.cfg["kill_grace"]
            deadline = time.monotonic() + grace + procs.JOIN_DEADLINE   # one deadline for all jobs
            for job in list(self.jobs):
                job.thread.join(max(0.0, deadline - time.monotonic()))   # the stop event was set by request_stop
                if job.thread.is_alive():
                    who = "base job" if job.kind == "base" else f"job {job.kind} #{job.task_id}"
                    print(f"warning: {who} did not stop", file=sys.stderr)
            if self.board is not None:
                self._guarded("collecting results", self._collect)
                self._guarded("closing the idle spans", self._close_idle_spans)
        finally:
            try:
                self._close_sender(max(self.cfg["kill_grace"], NOTIFY_FLUSH_MIN))
            finally:
                self._release()

    def wait(self, timeout: float) -> None:
        self._wake.wait(timeout)

    def run_forever(self) -> int:
        # signals are an extra soft stop (PD-26); the handler only counts them. Off the main thread (tests) a
        # handler cannot be installed: stop requests and the stop file still work.
        if threading.current_thread() is threading.main_thread():
            signals = platform.install_stop_handlers()
        else:
            signals = platform.StopRequest()
        self.start()
        try:
            while True:
                if signals.requested():
                    self.request_stop(now=False)   # a second signal is still soft: `artel stop --now` is the hard one
                self.tick()
                if self.stopping and (not self.jobs or self._stop_overdue()):
                    break
                self.wait(self.cfg["poll_interval"])
            self._emit(Event("runner_stopped"))
        except KeyboardInterrupt:
            self.request_stop(now=True)
        finally:
            self.shutdown()
        return 0

    # notifications ----------------------------------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        """Queued for the sender thread: a notifier can take ~45 s and must never hold up the tick. A raising
        notifier cannot undo or repeat the bookkeeping around it (no second FAIL, no second charge)."""
        if self._sender is None:
            self._sender = threading.Thread(target=self._send_loop, name="artel-notify", daemon=True)
            self._sender.start()
        self._outbox.put(event)

    def _send_loop(self) -> None:
        while True:
            event = self._outbox.get()
            try:
                if event is None:
                    return
                self.notify(event)
            except Exception:   # noqa: BLE001 - journaled by the tick thread (the board is not shared)
                self._notify_errors.append(traceback.format_exc())
            finally:
                self._outbox.task_done()

    def flush_notifications(self, timeout: float) -> bool:
        """Waits until every queued event was handed to the notifier; False if the time ran out."""
        deadline = time.monotonic() + timeout
        while self._outbox.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _close_sender(self, timeout: float) -> None:
        sender = self._sender
        if sender is None:
            return
        flushed = self.flush_notifications(timeout)
        if not flushed:
            print(f"warning: {self._outbox.unfinished_tasks} notification(s) not sent: the notifier did not finish"
                  f" within {timeout:g} s", file=sys.stderr)
        self._outbox.put(None)   # a sender still busy ends after its event; it is a daemon either way
        self._sender = None
        if not flushed and self.notify_kill is not None:
            try:
                self.notify_kill()   # a hung notify command: its process tree is terminated, later sends refused
            except Exception as exc:   # noqa: BLE001 - shutdown goes on
                print(f"warning: the notifier could not be stopped: {exc}", file=sys.stderr)
            sender.join(self.cfg["kill_grace"] + procs.JOIN_DEADLINE)

    def _journal_notify_errors(self) -> None:
        while self._notify_errors:
            tail = self._notify_errors.pop(0)
            self._record_tick_error("the notifier", tail)

    # workers ------------------------------------------------------------------------------------------------

    def _spawn(self, kind: str, task_id: int | None, slot: int, work: Callable[[Board, Job], object]) -> Job:
        stop = threading.Event()
        job = Job(kind=kind, task_id=task_id, slot=slot, thread=None, stop=stop,   # type: ignore[arg-type]
                  started_at=self.clock())

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
                job.finished = True
                self._wake.set()

        if task_id is not None:
            job.version = self.board.get_task(task_id).version
        job.thread = threading.Thread(target=body, name=f"artel-{kind}-{task_id}", daemon=True)
        self.jobs.append(job)
        self._touch(job)
        job.thread.start()
        return job

    def _touch(self, job: Job) -> None:
        now = self.clock()
        if job.task_id is not None:
            self.activity[job.task_id] = now
        if job.kind in ("stage", "verify", "base"):
            self.stage_activity[job.task_id or 0] = now

    def _skip_refs(self, job: Job, started: float) -> set[str]:
        """Branches of other tasks the runner touched since `started` or that have a job: not the stage's doing.
        The stage's own task is never in it (its branch is the stage's to move, and only for a writer)."""
        tasks = {t for t, at in list(self.activity.items()) if at >= started}
        tasks |= {j.task_id for j in list(self.jobs) if j.task_id is not None}
        tasks.discard(job.task_id)
        return {f"refs/heads/{task_branch(t)}" for t in tasks}

    def _touch_requested(self) -> None:
        """A request may move or delete the branch of its task: stamp it before the git effect (PD-66 N1)."""
        now = self.clock()
        for request in self.board.pending_requests():
            if request.task_id is not None:
                self.activity[request.task_id] = now

    def _leave_quietly(self, board: Board | None, slot: int) -> None:
        if board is None:
            return
        try:
            slots.leave_slot(self.repo, board, self.cfg, slot)
        except Exception:   # noqa: BLE001 - the job's own error is the one to report
            pass

    def _on_start(self, board: Board, job: Job):
        def on_start(kind: str, pid: int, group: int, fingerprint: str | None) -> None:
            if job.kind in ("base", "probe"):   # PD-34: the base job and the probe belong to no task
                board.set_meta("base_running" if job.kind == "base" else _PROBE_KEY,
                               json.dumps({"pid": pid, "group": group, "fingerprint": fingerprint,
                                           "slot": job.slot, "started_at": self.clock()}))
            else:
                if kind == "agent":
                    job.agent_started = True
                board.set_running(job.task_id, kind=kind, pid=pid, group=group, fingerprint=fingerprint,
                                  slot=job.slot)
        return on_start

    def _stage_work(self, board: Board, job: Job):
        task = board.get_task(job.task_id)
        try:
            plan = stages.plan_stage(self.repo, board, self.cfg, task, job.slot, base_env=self.child_env(),
                                     workers=self.workers)                                     # PD-71 a
        except agent_claude.CannotStartAgent as exc:   # owner 2026-10-21: agent_unavailable, not an internal error
            return stages.unstarted_stage(task, str(exc))
        started = self.clock()

        def skip_refs() -> set[str]:   # the branches the runner itself touched during the stage (PD-66)
            return self._skip_refs(job, started)

        def overlapping() -> set[int]:   # other agent stages, setups and verify jobs during this stage
            others = {t for t, at in list(self.stage_activity.items()) if at >= started}
            others |= {j.task_id or 0 for j in list(self.jobs) if j is not job and j.kind in ("stage", "verify", "base")}
            others.discard(job.task_id)
            return others
        run = stages.run_stage(self.repo, board, self.cfg, plan, job.slot, on_start=self._on_start(board, job),
                               on_rate_limit=self.on_rate_limit, stop_event=job.stop, skip_refs=skip_refs,
                               overlapping=overlapping)
        return plan, run

    def _verify_work(self, board: Board, job: Job, queued_at: float | None = None):
        task = board.get_task(job.task_id)
        return integrate.verify_task(self.repo, board, self.cfg, task, job.slot, on_start=self._on_start(board, job),
                                     stop_event=job.stop, env=self.child_env(), queued_at=queued_at)

    def _base_work(self, board: Board, job: Job):
        return integrate.base_update(self.repo, board, self.cfg, job.slot, on_start=self._on_start(board, job),
                                     stop_event=job.stop, env=self.child_env())

    # the tick -----------------------------------------------------------------------------------------------

    def tick(self) -> None:
        self._wake.clear()   # before collecting: a job that ends from here on sets it again, no lost wakeup
        self._tick_failed = False
        self._pick_inputs = None
        self._tick()
        self._guarded("the idle spans", self._update_idle)
        if not self._tick_failed:
            self._last_tick_error = None   # the same error later is recorded again

    def _tick(self) -> None:
        """Requests, collection, the integration tip and the pause end conditions run in every tick, paused or
        not (§3.1 Pauses). Verify, integration and the base job run unless a non-token-free kind is active
        (PD-54); agent stages start only with no pause at all."""
        self._guarded("the notifier", self._journal_notify_errors)
        self._guarded("applying requests", self._apply_requests)
        self._guarded("reading the stop file", self._read_stop_file)
        collect_failed = self._guarded("collecting results", self._collect)
        self._guarded("clearing orphaned running flags", self._clear_orphans)
        self._guarded("noting verify waits", self._note_verify_due)
        self._guarded("checking the integration branch", self._integration_moved)
        self._guarded("updating pauses", self.update_pauses)
        if collect_failed:
            return
        self._guarded("pruning logs", self._prune_logs_if_finished)
        gate: dict[str, bool] = {}
        if self._guarded("reading the pauses", lambda: gate.update(verify=pauses.blocks_verify(self.board),
                                                                   agents=pauses.blocks_agents(self.board))):
            return
        if gate["verify"]:
            return
        self._guarded("integrating", self._integrate_verified)
        if self.stopping:
            return
        if self._guarded("the base check", self._base_moved):
            return
        if gate["agents"]:
            self._guarded("picking work", lambda: self._pick_and_start(verify_only=True))
        else:
            self._guarded("picking work", self._pick_and_start)

    def _apply_requests(self) -> None:
        self._announced = set(pauses.active(self.board))   # kinds whose `paused` the human has already been sent
        try:
            self._touch_requested()
            requests.apply_pending(self.repo, self.board, self.cfg, self, now=self.clock())
            self._touch_requested()
            for kind in pauses.active(self.board):   # `artel pause`: every pause notifies (§3.1)
                if kind not in self._announced:
                    self._notify_paused(kind)
        finally:
            self._announced = None

    def _prune_logs_if_finished(self) -> None:
        if len(self.board.list_tasks(FINISHED_STATES)) != self._finished_count:
            self.prune_logs()

    def _read_stop_file(self) -> None:
        """PD-26: `artel stop` also writes the stop file; whichever the runner sees first stops it."""
        path = self.repo.stop_path
        if not path.exists():
            return
        try:
            mode = path.read_text(encoding="utf-8").strip()
        except OSError:
            mode = "soft"
        self.request_stop(now=mode == "now")
        try:
            path.unlink(missing_ok=True)
        except OSError:   # Windows: `artel stop` may still hold it open; stopping twice is harmless
            pass

    def _guarded(self, what: str, fn):
        """An unexpected error in one step of the tick must not end the runner; it is recorded once per text."""
        try:
            return fn() or False
        except Exception:   # noqa: BLE001
            self._record_tick_error(what, traceback.format_exc())
            return True   # pick nothing this tick

    def _record_tick_error(self, what: str, tail: str) -> None:
        self._tick_failed = True
        text = stages._clip_tail(f"internal error in {what}: ", tail)
        if self._last_tick_error == text:
            return
        self._last_tick_error = text
        try:
            journal.append(self.board, "FAIL", text, author="runner")
        except Exception as exc:   # noqa: BLE001 - e.g. the board is locked or gone: the runner keeps ticking
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            print(f"artel: internal error in {what}: {last} (not journaled: {exc})", file=sys.stderr)

    def _collect(self) -> None:
        self._apply_rate_events()   # events a finishing job queued come before its result is applied
        for job in self.jobs:
            if job.finished:
                job.thread.join(1.0)   # its work is over: collect it now, not one poll_interval later
        for job in [j for j in self.jobs if not j.thread.is_alive()]:
            self._touch(job)   # before it leaves `jobs`: no moment where its branch is neither live nor stamped
            self.jobs.remove(job)
            try:
                self._finish(job)
            except Exception:   # noqa: BLE001 - one task's bookkeeping must not stop the runner
                tail = traceback.format_exc()
                self._guarded("finishing a job", lambda: self._finish_failed(job, tail))
            self._touch(job)   # its git effects (integrate, branch delete) ran in `_finish`

    def _clear_orphans(self) -> None:
        """T22 controller review a: a task marked running with no job of this runner (its result could not be applied
        and even `clear_running` failed, e.g. the board was locked) is requeued without a counter, like reconcile
        does at start; a failing clear raises and is tried again next tick."""
        board = self.board
        live = {j.task_id for j in self.jobs if j.task_id is not None}
        for task in board.list_tasks():
            if not task.running or task.id in live:
                continue
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            board.clear_running(task.id)
            journal.append(board, "FAIL", f"running flag of {stage} cleared: its job ended but its result was not"
                                          " applied; requeued without a counter", author="runner", task_id=task.id,
                           stage=stage)
            self._emit(Event("error", task.id, task.title, task.state,
                             f"internal error in {stage}; see artel show {task.id}"))

    def _finish_failed(self, job: Job, tail: str) -> None:
        """The result of a job could not be applied. A task job is charged like any internal error, so the
        stage cannot run (and spend quota) forever; if even that fails the FAIL entry is all that is left."""
        board = self.board
        if job.kind == "base" or job.task_id is None:
            self._base_failed(tail, getattr(job.result, "log_path", None))
            return
        try:
            task = board.get_task(job.task_id)
            if task.version != job.version:   # a transition was already applied: charging again would double-charge
                self._record_finish_failure(job.task_id, tail)
                return
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            tr = stages.finish_internal_error(self.repo, board, self.cfg, job.task_id, tail,
                                              agent_started=job.kind == "stage" and job.agent_started)
            self._emit(Event("error", job.task_id, task.title, board.get_task(job.task_id).state,
                              f"internal error in {stage}; see artel show {job.task_id}"))
            self._after_transition(job.task_id, tr, "internal error")
            if job.kind == "stage":
                self._guarded("the failure breaker", lambda: self._agent_outcome(job, tr, None))
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
        if job.kind == "probe":
            try:
                board.del_meta(_PROBE_KEY)
                self._finish_probe(job)
                self._prune_probe_logs()
            except Exception:   # noqa: BLE001 - a probe belongs to no task: nothing else may be charged for it
                self._record_tick_error("the probe", traceback.format_exc())
            return
        if job.discarded:                             # PD-30: terminated for a request, nothing to apply
            if job.task_id is not None:
                board.clear_running(job.task_id)
            return
        if job.kind == "base":
            board.del_meta("base_running")
            if job.error is not None:
                self._base_failed(job.error)
                return
            if job.result is None:
                return
            moves = job.result.kind in ("fast_forward", "merged")
            if moves and self._integration_moved():
                return   # PD-65 d: the result is discarded; after resume the base check runs again
            was_paused = "base_conflict" in pauses.active(board)
            try:
                outcome = integrate.finish_base_update(self.repo, board, self.cfg, job.result, now=self.clock())
            except Exception:
                if moves:
                    self._keep_tip_if_moved_to(job.result.new)   # it may have failed after moving the ref itself
                raise
            if outcome == "applied":
                board.set_meta(_TIP_KEY, job.result.new)
            elif outcome == "paused" and not was_paused:
                self._notify_paused("base_conflict")
            return
        if job.busy is not None:                      # PD-8: skipped, nothing applied, no attempt
            board.clear_running(job.task_id)
            return
        if job.error is not None:
            task = board.get_task(job.task_id)
            stage = stages.STATE_TO_STAGE.get(task.state, task.state)
            tr = stages.finish_internal_error(self.repo, board, self.cfg, job.task_id, job.error,
                                              agent_started=job.kind == "stage" and job.agent_started)
            self._emit(Event("error", job.task_id, task.title, board.get_task(job.task_id).state,
                              f"internal error in {stage}; see artel show {job.task_id}"))   # PD-38: no traceback
            self._after_transition(job.task_id, tr, "internal error")
            if job.kind == "stage":
                self._guarded("the failure breaker", lambda: self._agent_outcome(job, tr, None))
            return
        if job.kind == "stage":
            plan, run = job.result
            tr = stages.finish_stage(self.repo, board, self.cfg, job.task_id, plan, run)
            detail = f"setup {run.setup}"
            # T35-2 F1: a skipped setup (inputs unchanged) got past setup too; a conflict fix's `not_run` did not
            if run.setup in ("ok", "skipped"):
                self._setup_worked(job.started_at)
            self._after_transition(job.task_id, tr, detail, slot=job.slot)
            self._guarded("the failure breaker", lambda: self._agent_outcome(job, tr, run))
            return
        try:
            tr = integrate.finish_verify(self.repo, board, self.cfg, job.task_id, job.result)
        finally:
            board.clear_running(job.task_id)   # the job is over, whatever happened to the transition
        self._after_transition(job.task_id, tr, job.result.detail, slot=job.slot)

    def _base_failed(self, error: str, log_path: Path | None = None) -> None:
        """PD-34: the traceback goes into the runner FAIL entry only; the pause and its event stay one line."""
        board = self.board
        journal.append(board, "FAIL", stages._clip_tail("internal error in the base job: ", error), author="runner")
        detail = "base update failed, see the FAIL entry"
        if log_path is not None:
            detail += f"; log {integrate._rel(self.repo, log_path)}"
        self.pause("base_conflict", detail)

    def _after_transition(self, task_id: int, tr, detail: str, slot: int | None = None) -> None:
        if tr is None:
            return
        board = self.board
        task = board.get_task(task_id)
        if "pause_setup_failed" in tr.effects:
            self._pause_setup_failed(task_id, detail, slot)
        if tr.to_state == "awaiting_accept":
            self._emit(Event("awaiting_accept", task_id, task.title, tr.to_state, tr.cause or None))
        if tr.to_state in BLOCKING_STATES:
            self._emit(Event(tr.to_state, task_id, task.title, tr.to_state, tr.cause or None))

    def _integrate_verified(self) -> None:
        board = self.board
        live = {j.task_id for j in self.jobs if j.task_id is not None}
        for task in board.list_tasks(["verified"]):
            if task.running or task.id in live or self._integrate_skip.get(task.id, 0) > self.clock():
                continue
            if self._integration_moved():
                return   # PD-65 d: the task stays verified; nothing is fast-forwarded over a foreign commit
            verified = board.get_meta(integrate._verified_key(task.id))   # integrate_task deletes it
            try:
                outcome = integrate.integrate_task(self.repo, board, self.cfg, task.id)
            except Exception:   # noqa: BLE001 - e.g. a human deleted the branch: record once, retry in a minute
                self._integrate_skip[task.id] = self.clock() + 60
                self._record_finish_failure(task.id, traceback.format_exc())
                self._keep_tip_if_moved_to(verified)   # it may have failed after moving the ref itself
                continue
            if outcome == "done":
                board.set_meta(_TIP_KEY, verified)
                self._emit(Event("done", task.id, task.title, "done"))

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

    def _pick_and_start(self, verify_only: bool = False) -> None:
        board = self.board
        free = self._free_slots()
        if not free:
            return
        tasks = board.list_tasks()
        if "vet" not in stages.SUPPORTED_STAGES:   # they wait for Task 21; filtering after select would starve open tasks
            tasks = [t for t in tasks if t.state != "proposed"]
        busy = {j.task_id for j in self.jobs if j.task_id is not None}
        # PD-8: a held task is never a candidate, but an in-flight one still holds its area; no slot wasted.
        # A held open task holds no area, and as busy it would count as a running holder in pick.select.
        held = self._held_elsewhere(tasks)   # before any filter: a filtered-out task is not "released"
        if verify_only:   # a token-free pause (PD-54): running tasks still hold their areas, nothing else starts
            tasks = [t for t in tasks if t.state == "verifying" or t.running or t.id in busy]
        tasks = [t for t in tasks if not (t.id in held and t.state == "open")]
        busy |= {t.id for t in tasks if t.id in held}
        deps = board.all_deps()
        selected = pick.select(tasks, deps, free_slots=len(free),
                               verify_busy=any(j.kind == "verify" for j in self.jobs), busy=busy)
        self._pick_inputs = (tasks, deps, set(busy))   # PD-71 b: idle_reason sees exactly what select saw
        for task in selected:
            if not free:
                break
            task = self._start_transition(task)
            if task.state == "verifying":
                queued_at = self._verify_due.pop(task.id, self.clock())                 # PD-71 c
                self._spawn("verify", task.id, free.pop(0),
                            lambda board_, job, q=queued_at: self._verify_work(board_, job, q))
            elif self._at_cap(task):
                continue   # R39 applied: no run, no slot
            else:
                self._spawn("stage", task.id, free.pop(0), self._stage_work)

    def _at_cap(self, task) -> bool:
        """§3.1 R39: a task whose `stage_runs` reached `max_stage_runs` goes `stuck` instead of starting an agent
        stage (no run, no slot, no agent call). A human command that moves it sets `stage_runs` to 0."""
        limits = transitions.Limits.from_cfg(task, self.cfg)
        if task.state not in transitions.STAGE_STATES or task.stage_runs < limits.max_stage_runs:
            return False
        board = self.board
        stage = stages.STATE_TO_STAGE.get(task.state, task.state)
        with board.tx():
            tr = transitions.apply(board, task.id, "due_at_cap", limits)
            journal.append(board, "VERDICT", stages._clip(f"STUCK {stage} r{task.round}: {tr.cause}"),
                           author="runner", task_id=task.id, stage=stage, round=task.round)
        self._after_transition(task.id, tr, "")
        return True

    def _note_verify_due(self) -> None:
        """PD-71 c: the time each task was first seen due for verify without a job; the verify run records it."""
        live = {j.task_id for j in self.jobs if j.task_id is not None}
        due = {t.id for t in self.board.list_tasks(["verifying"]) if not t.running and t.id not in live}
        now = self.clock()
        self._verify_due = {i: self._verify_due.get(i, now) for i in due}

    # idle spans (PD-71 b) ---------------------------------------------------------------------------------------

    def _update_idle(self) -> None:
        """After the pick: every slot without a job gets the reason it is idle; a span is written only when a
        slot's reason changes and closed when the slot gets a job."""
        if self.stopping or self.board is None:
            return
        board, now = self.board, self.clock()
        used = {j.slot for j in self.jobs}
        for n in [n for n in self._idle if n in used]:
            board.close_idle(n, now)
            del self._idle[n]
        idle = [n for n in range(1, self.cfg["max_agents"] + 1) if n not in used]
        if not idle:
            return
        active = pauses.active(board)
        if active:
            reason = f"paused:{next(iter(active))}"   # pauses.active is in pauses.KINDS order
        elif self._pick_inputs is not None:
            tasks, deps, busy = self._pick_inputs
            reason = pick.idle_reason(tasks, deps, busy=busy)
        else:
            return   # nothing was picked this tick (a base update, a failed step): the spans stay as they are
        for n in idle:
            if self._idle.get(n) != reason:
                board.open_idle(n, reason, now)   # closes the slot's previous span
                self._idle[n] = reason

    def _close_idle_spans(self) -> None:
        now = self.clock()
        for n in list(self._idle):
            self.board.close_idle(n, now)
            del self._idle[n]

    # the failure breaker and the agent_unavailable streak (spec §5 step 6, PD-58, PD-60, PD-61) -----------------

    def _agent_outcome(self, job: Job, tr, run) -> None:
        """After a classified agent outcome. Requeues (tr None) and setup failures neither count nor reset;
        `agent_unavailable` pauses (or escalates) and leaves the failure count alone; every other outcome resets
        the unavailable streak, and counts for the breaker when it increments `attempts`, else resets it."""
        if tr is None or tr.event == "setup_failed":
            return
        if tr.event == "agent_unavailable":
            self._agent_unavailable(job, run)
            return
        self._reset_unavailable_streak()
        failed = tr.event in ("stage_failed", "internal_error") or \
            (tr.event == "ready_no_commits" and "attempts" in tr.updates)   # R17r; R15b asks a human instead
        streak = self._json_meta(_FAILURE_STREAK, {})
        count = streak.get("count") if isinstance(streak.get("count"), int) else 0
        ids = [i for i in streak.get("tasks", []) if isinstance(i, int)] if isinstance(streak.get("tasks"), list) else []
        if not failed:
            if count or ids:
                self.board.set_meta(_FAILURE_STREAK, json.dumps({"count": 0, "tasks": []}))
            return
        count += 1
        if job.task_id not in ids:
            ids.append(job.task_id)
        self.board.set_meta(_FAILURE_STREAK, json.dumps({"count": count, "tasks": ids}))
        if (count >= self.cfg["max_consecutive_failures"] and len(ids) >= BREAKER_MIN_TASKS
                and "failures" not in pauses.active(self.board)):
            self.pause("failures", f"{count} failed stages in a row: " + ", ".join(f"#{i}" for i in ids),
                       setting="max_consecutive_failures")

    def _reset_unavailable_streak(self) -> None:
        self._unavailable_reset_at = self.clock()   # a new outage needs a new passing probe to qualify
        self._unavailable_counted_probe = None
        streak = self._json_meta(_UNAVAILABLE_STREAK, {})
        if streak.get("count") or streak.get("signature"):
            self.board.set_meta(_UNAVAILABLE_STREAK, json.dumps({"signature": "", "count": 0}))

    def _passing_probe_at(self, started_at: float) -> float | None:
        """PD-61: the time of the passing probe the run started after, if that probe passed since the streak was
        last reset; else None."""
        last = self._json_meta("last_probe", {})
        at = last.get("at")
        if last.get("outcome") == "ok" and _is_number(at) and self._unavailable_reset_at < at <= started_at:
            return at
        return None

    def _agent_unavailable(self, job: Job, run) -> None:
        """R38 was applied by finish_stage (requeue, no counter, branch kept). Pause `agent_unavailable` until a
        probe passes; the same signature `max_consecutive_failures` times in a row, each run started after a
        passing probe, is a permanent fault: pause `failures` instead (PD-61; one task is enough)."""
        signature = (run.signature if run is not None else None) or "unknown"
        probe_at = self._passing_probe_at(job.started_at)
        if probe_at is not None:
            streak = self._json_meta(_UNAVAILABLE_STREAK, {})
            count = streak.get("count") if isinstance(streak.get("count"), int) else 0
            # controller decision (T37 review m1): at most one run per passing probe counts — the runs started in
            # parallel after one probe are one piece of evidence, not `max_agents` of them. In memory: a restart
            # moves `_unavailable_reset_at`, so a probe from before it never qualifies anyway.
            if probe_at != self._unavailable_counted_probe:
                count = count + 1 if streak.get("signature") == signature else 1
                self.board.set_meta(_UNAVAILABLE_STREAK, json.dumps({"signature": signature, "count": count}))
                self._unavailable_counted_probe = probe_at   # after the write: a failed write counts nothing
            if count >= self.cfg["max_consecutive_failures"]:
                if "failures" not in pauses.active(self.board):
                    self.pause("failures", f"agent unavailable {count} times with {signature} although probes pass"
                                           " — check models.* and agent_command", setting="max_consecutive_failures")
                return
        self.pause("agent_unavailable", signature, until=None)

    def _held_elsewhere(self, tasks) -> set[int]:
        """Tasks whose branch a human (any non-slot worktree) has checked out; journaled once when a task becomes
        held and once when it is released (PD-8)."""
        self.repo.git("worktree", "prune", check=False)   # a deleted human worktree must not hold forever
        mine = {Path(s.path).resolve() for s in self.board.list_slots()}
        by_branch = {wt.branch: wt.path for wt in self.repo.worktrees()
                     if wt.branch and wt.path.resolve() not in mine}
        held = {t.id: by_branch[task_branch(t.id)] for t in tasks if task_branch(t.id) in by_branch}
        for task_id, path in held.items():
            if task_id not in self._held:
                journal.append(self.board, "FACT", f"held: {task_branch(task_id)} checked out at {path}",
                               author="runner", task_id=task_id)
        for task_id in [i for i in self._held if i not in held]:
            journal.append(self.board, "FACT", f"released: {task_branch(task_id)}", author="runner",
                           task_id=task_id)
        self._held = held
        return set(held)

    def _start_transition(self, task):
        board = self.board
        if task.state in ("open", "proposed"):
            transitions.apply(board, task.id, "pick", transitions.Limits.from_cfg(task, self.cfg))
            task = board.get_task(task.id)
        if task.answered:
            board.update_task(task.id, expect_state=task.state, expect_version=task.version, answered=False)
            task = board.get_task(task.id)
        return task


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _size(n: int) -> str:
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f}{unit}"
    return f"{n}B"


def run_command(args) -> int:
    if requests._refused("run"):   # PD-65 b
        return 2
    repo = Repo.discover(Path.cwd())
    cli = {"max_agents": (args.n, "flag -n")} if getattr(args, "n", None) else None
    cfg = config_mod.load_config(repo, cli=cli)
    dispatcher = notify_mod.Dispatcher.from_config(cfg, os.environ)
    return Runner(repo, cfg, notify=dispatcher.emit, notify_kill=dispatcher.kill).run_forever()
